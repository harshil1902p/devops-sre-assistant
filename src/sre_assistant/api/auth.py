"""Entra ID (Azure AD) bearer-token validation and app-role authorization.

The API never sees passwords: clients get an access token from Entra ID for our API's app registration
(audience) and send `Authorization: Bearer <jwt>`. We verify, offline, using Entra's published signing keys (JWKS):
signature (RS256 only), issuer (our tenant), audience (our API), expiry/not-before. Authorization uses
**app roles** (`roles` claim) assigned to users/groups in Entra: SRE.Diagnose, SRE.Approve.
"""

from dataclasses import dataclass, field

import jwt

ROLE_DIAGNOSE = "SRE.Diagnose"
ROLE_APPROVE = "SRE.Approve"


@dataclass(frozen=True)
class Principal:
    subject: str  # stable object id (oid); never use the display name for authorization
    name: str
    roles: frozenset[str] = field(default_factory=frozenset)

    def has(self, role: str) -> bool:
        return role in self.roles


DEV_PRINCIPAL = Principal("local-dev", "local developer", frozenset({ROLE_DIAGNOSE, ROLE_APPROVE}))


class AuthError(Exception):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail
        super().__init__(detail)


class EntraValidator:
    def __init__(self, tenant_id: str, audience: str, jwks_client=None, leeway_s: int = 60):
        self.tenant_id, self.leeway = tenant_id, leeway_s
        # Tokens may carry the app id URI or the bare client id as `aud`, depending on how they were requested.
        self.audiences = [audience] + ([audience.removeprefix("api://")] if audience.startswith("api://") else [])
        # v2.0 and v1.0 endpoints use different issuer formats; accept exactly these two for OUR tenant.
        self.issuers = [f"https://login.microsoftonline.com/{tenant_id}/v2.0", f"https://sts.windows.net/{tenant_id}/"]
        self.jwks = jwks_client or jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys", cache_keys=True, lifespan=3600
        )

    def validate(self, token: str) -> Principal:
        try:
            key = self.jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token, key, algorithms=["RS256"],  # never accept "none"/HS256 (algorithm confusion)
                audience=self.audiences, issuer=self.issuers, leeway=self.leeway,
                options={"require": ["exp", "iat", "aud", "iss", "sub"]},
            )  # fmt: skip
        except jwt.ExpiredSignatureError as e:
            raise AuthError(401, "token expired") from e
        except (jwt.PyJWTError, jwt.PyJWKClientError) as e:
            raise AuthError(401, f"invalid token: {type(e).__name__}") from e
        if claims.get("tid") not in (None, self.tenant_id):
            raise AuthError(401, "wrong tenant")
        return Principal(
            subject=claims.get("oid") or claims["sub"],
            name=claims.get("preferred_username") or claims.get("name") or claims.get("appid") or "unknown",
            roles=frozenset(claims.get("roles", [])),
        )


def bearer_token(header: str | None) -> str:
    if not header or not header.lower().startswith("bearer "):
        raise AuthError(401, "missing bearer token")
    return header[7:].strip()

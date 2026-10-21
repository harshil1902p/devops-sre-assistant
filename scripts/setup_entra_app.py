"""Create (idempotently) the Entra ID app registration that protects the agent API.

uv run python scripts/setup_entra_app.py                 # creates app + roles, assigns BOTH roles to you
uv run python scripts/setup_entra_app.py --roles SRE.Diagnose

What it sets up:
  - app roles SRE.Diagnose / SRE.Approve (the API authorizes on the `roles` claim),
  - identifier URI api://<appId>, a `user_impersonation` scope, v2 access tokens,
  - Azure CLI pre-authorized, so `az account get-access-token --resource api://<appId>` works for testing,
  - "assignment required": users without a role can't even get a token for the API.
Prints the app (client) id to put in SRE_API_AUDIENCE / the Bicep `apiAudience` parameter. No secrets are created.
"""

import argparse
import json
import subprocess
import uuid

GRAPH = "https://graph.microsoft.com/v1.0"
AZURE_CLI_APP_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"  # well-known public client id of the Azure CLI
NAME = "sre-assistant-api"
ROLES = {
    "SRE.Diagnose": "Run diagnoses and read own incident threads",
    "SRE.Approve": "Approve or reject remediation actions proposed by the agent",
}


def az(*args: str, body: dict | None = None):
    cmd = ["az", *args, "-o", "json"]
    if body is not None:
        cmd += ["--headers", "Content-Type=application/json", "--body", json.dumps(body)]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    return json.loads(out) if out.strip() else None


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--roles", nargs="+", default=list(ROLES), choices=list(ROLES))
    a = p.parse_args()

    apps = az("rest", "--method", "GET", "--url", f"{GRAPH}/applications?$filter=displayName eq '{NAME}'")["value"]
    app = apps[0] if apps else az("rest", "--method", "POST", "--url", f"{GRAPH}/applications",
                                  body={"displayName": NAME, "signInAudience": "AzureADMyOrg"})  # fmt: skip
    app_id, obj_id = app["appId"], app["id"]

    existing_roles = {r["value"]: r for r in app.get("appRoles", [])}
    app_roles = [existing_roles.get(v) or {"id": str(uuid.uuid4()), "value": v, "displayName": v, "description": d,
                 "allowedMemberTypes": ["User"], "isEnabled": True} for v, d in ROLES.items()]  # fmt: skip
    scopes = (app.get("api") or {}).get("oauth2PermissionScopes") or [{
        "id": str(uuid.uuid4()), "value": "user_impersonation", "type": "User", "isEnabled": True,
        "adminConsentDisplayName": "Access the SRE assistant API", "adminConsentDescription": "Call the SRE agent API",
        "userConsentDisplayName": "Access the SRE assistant API", "userConsentDescription": "Call the SRE agent API"}]  # fmt: skip
    url = f"{GRAPH}/applications/{obj_id}"
    az("rest", "--method", "PATCH", "--url", url, body={
        "identifierUris": [f"api://{app_id}"], "appRoles": app_roles,
        "api": {"requestedAccessTokenVersion": 2, "oauth2PermissionScopes": scopes}})  # fmt: skip
    # Pre-authorization must reference a scope that already exists, hence a second PATCH.
    az("rest", "--method", "PATCH", "--url", url, body={"api": {"preAuthorizedApplications": [
        {"appId": AZURE_CLI_APP_ID, "delegatedPermissionIds": [scopes[0]["id"]]}]}})  # fmt: skip

    sps = az("rest", "--method", "GET", "--url", f"{GRAPH}/servicePrincipals?$filter=appId eq '{app_id}'")["value"]
    sp = (
        sps[0] if sps else az("rest", "--method", "POST", "--url", f"{GRAPH}/servicePrincipals", body={"appId": app_id})
    )
    az("rest", "--method", "PATCH", "--url", f"{GRAPH}/servicePrincipals/{sp['id']}",
       body={"appRoleAssignmentRequired": True})  # fmt: skip

    me = az("ad", "signed-in-user", "show")["id"]
    assigned = {x["appRoleId"] for x in az("rest", "--method", "GET", "--url",
                f"{GRAPH}/servicePrincipals/{sp['id']}/appRoleAssignedTo")["value"] if x["principalId"] == me}  # fmt: skip
    for role in app_roles:
        if role["value"] in a.roles and role["id"] not in assigned:
            az("rest", "--method", "POST", "--url", f"{GRAPH}/servicePrincipals/{sp['id']}/appRoleAssignedTo",
               body={"principalId": me, "resourceId": sp["id"], "appRoleId": role["id"]})  # fmt: skip
    print(f"app (client) id: {app_id}\nroles assigned to you: {', '.join(a.roles)}")
    print(f"token for testing: az account get-access-token --resource api://{app_id} --query accessToken -o tsv")


if __name__ == "__main__":
    main()

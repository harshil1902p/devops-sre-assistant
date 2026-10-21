"""Phase 8 API tests: real JWT validation (locally generated RSA keys), RBAC, four-eyes, rate limiting, SSE."""

import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from sre_assistant.api.app import create_app
from sre_assistant.api.auth import ROLE_APPROVE, ROLE_DIAGNOSE, AuthError, EntraValidator
from sre_assistant.api.ratelimit import TokenBucket
from sre_assistant.api.service import AgentService
from sre_assistant.config import Settings

TENANT, AUD = "11111111-1111-1111-1111-111111111111", "api://sre-assistant"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJwks:
    def get_signing_key_from_jwt(self, token):
        return SimpleNamespace(key=KEY.public_key())


def token(roles=(ROLE_DIAGNOSE,), oid="user-a", key=KEY, alg="RS256", **over) -> str:
    now = int(time.time())
    claims = {"iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "aud": AUD, "sub": oid, "oid": oid,
              "tid": TENANT, "iat": now, "nbf": now, "exp": now + 600, "roles": list(roles),
              "preferred_username": f"{oid}@contoso.com"} | over  # fmt: skip
    return jwt.encode(claims, key, algorithm=alg)


VALIDATOR = EntraValidator(TENANT, AUD, jwks_client=FakeJwks())


# ------------------------------------------------------------------ validator
def test_valid_token_maps_to_principal():
    p = VALIDATOR.validate(token(roles=[ROLE_DIAGNOSE, ROLE_APPROVE]))
    assert p.subject == "user-a" and p.has(ROLE_APPROVE) and p.name == "user-a@contoso.com"


@pytest.mark.parametrize(
    "bad",
    [
        lambda: token(key=OTHER_KEY),  # forged signature
        lambda: token(exp=int(time.time()) - 3600),  # expired
        lambda: token(aud="api://someone-else"),  # token for another API
        lambda: token(iss="https://login.microsoftonline.com/evil/v2.0"),  # another tenant's issuer
        lambda: jwt.encode({"sub": "x", "aud": AUD}, "s" * 32, algorithm="HS256"),  # algorithm confusion
    ],
)
def test_invalid_tokens_rejected(bad):
    with pytest.raises(AuthError) as e:
        VALIDATOR.validate(bad())
    assert e.value.status == 401


def test_bare_client_id_audience_accepted():
    assert VALIDATOR.validate(token(aud="sre-assistant")).subject == "user-a"


def test_token_bucket():
    t = [0.0]
    b = TokenBucket(per_minute=2, clock=lambda: t[0])
    assert b.take("a") == 0 and b.take("a") == 0
    assert b.take("a") == pytest.approx(30.0)  # 2/min -> next token in 30 s
    assert b.take("b") == 0  # per principal
    t[0] = 30.0
    assert b.take("a") == 0


# ----------------------------------------------------------- service + app
class FakeGraph:
    """Mimics the compiled LangGraph surface the service uses; a question containing 'restart' pauses for approval."""

    def __init__(self):
        self.state: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}

    async def aget_state(self, config):
        tid = config["configurable"]["thread_id"]
        interrupts = [SimpleNamespace(value=self.pending[tid])] if tid in self.pending else []
        return SimpleNamespace(values=self.state.get(tid, {}), interrupts=interrupts)

    async def astream(self, inputs, config, stream_mode):
        from langchain_core.messages import AIMessage, HumanMessage

        tid = config["configurable"]["thread_id"]
        st = self.state.setdefault(tid, {"messages": [], "llm_calls": []})
        if isinstance(inputs, dict):
            q = inputs["messages"][0][1]
            st["messages"] += [HumanMessage(q), AIMessage("diagnosis: pool exhausted")]
            st["llm_calls"].append({"input_tokens": 100, "output_tokens": 10, "latency_ms": 5})
            st["diagnosis"] = {"status": "degraded"}
            yield {"agent": {"messages": []}}
            if "restart" in q:
                self.pending[tid] = {"action": "restart_service", "service": "auth-service"}
        else:  # Command(resume=decision)
            decision = inputs.resume
            self.pending.pop(tid)
            st["remediation"] = {"status": "executed" if decision["approved"] else "rejected",
                                 "approver": decision["approver"]}  # fmt: skip
            yield {"approval": {}}


def client(**settings):
    s = Settings(api_auth="entra", entra_tenant_id=TENANT, api_audience=AUD, llm_provider="ollama", **settings)
    app = create_app(s, AgentService(s, graph=FakeGraph()), VALIDATOR)
    return TestClient(app)


def auth(**kw):
    return {"authorization": f"Bearer {token(**kw)}"}


def test_health_and_auth_required():
    with client() as c:
        assert c.get("/healthz").json() == {"status": "ok"}
        assert c.get("/readyz").status_code == 200
        r = c.post("/v1/diagnose", json={"question": "auth-service 500s"})
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        assert c.post("/v1/diagnose", json={"question": "x 500s"}, headers=auth(roles=[])).status_code == 403


def test_diagnose_json_and_thread_ownership():
    with client() as c:
        r = c.post("/v1/diagnose", json={"question": "auth-service 500s"}, headers=auth())
        body = r.json()
        assert r.status_code == 200 and body["status"] == "completed" and "pool" in body["answer"]
        assert r.headers["cache-control"] == "no-store" and r.headers["x-request-id"]
        tid = body["thread"]
        assert c.get(f"/v1/threads/{tid}", headers=auth()).status_code == 200
        # another user can't read or continue it, and learns nothing about its existence
        assert c.get(f"/v1/threads/{tid}", headers=auth(oid="user-b")).status_code == 404
        r = c.post("/v1/diagnose", json={"question": "and now?", "thread": tid}, headers=auth(oid="user-b"))
        assert r.status_code == 404


def test_input_validation():
    with client() as c:
        h = auth()
        assert c.post("/v1/diagnose", json={"question": "x" * 3000}, headers=h).status_code == 422
        assert c.post("/v1/diagnose", json={"question": "hi there", "service": "a b"}, headers=h).status_code == 422
        assert c.post("/v1/diagnose", json={"question": "hi there", "thread": "../x"}, headers=h).status_code == 422


def test_four_eyes_approval_flow():
    with client() as c:
        requester = auth(roles=[ROLE_DIAGNOSE, ROLE_APPROVE], oid="alice")
        body = c.post("/v1/diagnose", json={"question": "restart auth-service"}, headers=requester).json()
        assert body["status"] == "approval_required" and body["pending_action"]["action"] == "restart_service"
        tid = body["thread"]
        # Diagnose-only users can't approve; the requester can't approve their own action
        assert (
            c.post(f"/v1/threads/{tid}/approval", json={"approved": True}, headers=auth(oid="bob")).status_code == 403
        )
        r = c.post(f"/v1/threads/{tid}/approval", json={"approved": True}, headers=requester)
        assert r.status_code == 403 and "four-eyes" in r.json()["detail"]
        approver = auth(roles=[ROLE_DIAGNOSE, ROLE_APPROVE], oid="carol")
        r = c.post(f"/v1/threads/{tid}/approval", json={"approved": True, "comment": "ok"}, headers=approver)
        assert r.status_code == 200 and r.json()["remediation"] == {
            "status": "executed",
            "approver": "carol@contoso.com",
        }
        # nothing pending any more
        assert c.post(f"/v1/threads/{tid}/approval", json={"approved": True}, headers=approver).status_code == 409


def test_requester_may_reject_own_action():
    with client() as c:
        h = auth(roles=[ROLE_DIAGNOSE, ROLE_APPROVE], oid="alice")
        tid = c.post("/v1/diagnose", json={"question": "restart it"}, headers=h).json()["thread"]
        r = c.post(f"/v1/threads/{tid}/approval", json={"approved": False}, headers=h)
        assert r.status_code == 200 and r.json()["remediation"]["status"] == "rejected"


def test_rate_limit_per_principal():
    with client(api_rate_per_minute=2) as c:
        h = auth()
        assert [c.post("/v1/diagnose", json={"question": "q 500s"}, headers=h).status_code for _ in range(3)] == [
            200, 200, 429]  # fmt: skip
        assert c.post("/v1/diagnose", json={"question": "q 500s"}, headers=auth(oid="other")).status_code == 200


def test_sse_stream():
    with client() as c:
        h = auth() | {"accept": "text/event-stream"}
        with c.stream("POST", "/v1/diagnose", json={"question": "auth-service 500s"}, headers=h) as r:
            text = "".join(r.iter_text())
        assert r.headers["content-type"].startswith("text/event-stream")
        events = [line.split(": ", 1)[1] for line in text.splitlines() if line.startswith("event: ")]
        assert events[0] == "started" and "progress" in events and events[-1] == "result"


def test_dev_mode_without_auth():
    s = Settings(api_auth="disabled", llm_provider="ollama")
    with TestClient(create_app(s, AgentService(s, graph=FakeGraph()))) as c:
        assert c.post("/v1/diagnose", json={"question": "auth-service 500s"}).status_code == 200


def test_mcp_subprocess_env_forwards_identity_but_not_arbitrary_secrets():
    from sre_assistant.agent.mcp_tools import mcp_subprocess_env

    env = mcp_subprocess_env({"IDENTITY_ENDPOINT": "http://localhost:42356/msi/token", "IDENTITY_HEADER": "h",
                              "AZURE_CLIENT_ID": "c", "AZURE_FEDERATED_TOKEN_FILE": "/var/run/token",
                              "SRE_TELEMETRY_BACKEND": "azure_monitor", "GITHUB_TOKEN": "ghp_x", "AWS_SECRET": "y"})  # fmt: skip
    assert {"IDENTITY_ENDPOINT", "IDENTITY_HEADER", "AZURE_CLIENT_ID", "AZURE_FEDERATED_TOKEN_FILE",
            "SRE_TELEMETRY_BACKEND"} <= set(env)  # fmt: skip
    assert "GITHUB_TOKEN" not in env and "AWS_SECRET" not in env

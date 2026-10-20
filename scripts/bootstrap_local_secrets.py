"""Generate local-only credentials for a fresh clone. Never overwrites existing files.

    uv run python scripts/bootstrap_local_secrets.py

Creates (all gitignored):
  demo/.env                              AUTH_DB_PASSWORD, AUTH_JWT_SIGNING_KEY
  infra/.env                             VECTORDB_PASSWORD
  .env                                   from .env.example with SRE_DATABASE_URL filled in
  demo/auth-service/config/app.yaml      rendered from app.example.yaml (contains secrets on purpose:
                                         the MCP server must prove it redacts them)
"""

import re
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return dict(
        line.split("=", 1) for line in path.read_text().splitlines() if "=" in line and not line.startswith("#")
    )


def _write_once(path: Path, content: str) -> None:
    if path.exists():
        print(f"keep    {path.relative_to(ROOT)} (already exists)")
        return
    path.write_text(content)
    path.chmod(0o600)
    print(f"created {path.relative_to(ROOT)}")


def main() -> None:
    _write_once(ROOT / "demo/.env", f"AUTH_DB_PASSWORD={secrets.token_urlsafe(18)}\n"
                                    f"AUTH_JWT_SIGNING_KEY={secrets.token_hex(24)}\n")  # fmt: skip
    _write_once(ROOT / "infra/.env", f"VECTORDB_PASSWORD={secrets.token_urlsafe(18)}\n")

    demo, infra = _read_env(ROOT / "demo/.env"), _read_env(ROOT / "infra/.env")
    url = f"postgresql://sre:{infra['VECTORDB_PASSWORD']}@localhost:5433/sre"
    example = (ROOT / ".env.example").read_text()
    _write_once(ROOT / ".env", re.sub(r"(?m)^SRE_DATABASE_URL=.*$", f"SRE_DATABASE_URL={url}", example))

    tpl = (ROOT / "demo/auth-service/config/app.example.yaml").read_text()
    rendered = tpl.replace("${AUTH_DB_PASSWORD}", demo["AUTH_DB_PASSWORD"]).replace(
        "${AUTH_JWT_SIGNING_KEY}", demo["AUTH_JWT_SIGNING_KEY"]
    )
    _write_once(ROOT / "demo/auth-service/config/app.yaml", rendered)


if __name__ == "__main__":
    main()

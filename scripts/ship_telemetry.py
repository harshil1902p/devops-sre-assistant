"""Ship local Docker telemetry (demo "staging") to Azure Monitor so the cloud backend can query it with KQL.

uv run python scripts/ship_telemetry.py --once           # one batch (last 15 min backfill)
uv run python scripts/ship_telemetry.py --interval 30    # keep shipping (Ctrl-C to stop)

Cost: Log Analytics ingestion is per GB; the workspace has a 0.1 GB/day cap (see infra/azure/modules/platform.bicep).
"""

import argparse
import logging
import time

from sre_assistant.config import get_settings
from sre_assistant.mcp_server.backends.docker_backend import DockerBackend
from sre_assistant.shipping import Shipper


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", type=int, default=30, help="seconds between batches")
    p.add_argument("--backfill-minutes", type=int, default=15)
    a = p.parse_args()
    logging.basicConfig(level=logging.WARNING)
    s = get_settings()
    if not (s.logs_ingestion_endpoint and s.logs_ingestion_rule_id):
        raise SystemExit("set SRE_LOGS_INGESTION_ENDPOINT and SRE_LOGS_INGESTION_RULE_ID (Bicep output logsIngestion)")

    from azure.identity import DefaultAzureCredential
    from azure.monitor.ingestion import LogsIngestionClient

    client = LogsIngestionClient(
        s.logs_ingestion_endpoint, DefaultAzureCredential(exclude_interactive_browser_credential=True)
    )
    shipper = Shipper(DockerBackend(s.docker_host, event_store_dsn=s.database_url.get_secret_value()), client,
                      s.logs_ingestion_rule_id, a.backfill_minutes)  # fmt: skip
    while True:
        r = shipper.run_once()
        print(f"{time.strftime('%H:%M:%S')} shipped logs={r.logs} events={r.events} health={r.health}"
              + (f" errors={r.errors}" if r.errors else ""), flush=True)  # fmt: skip
        if a.once:
            break
        time.sleep(a.interval)


if __name__ == "__main__":
    main()

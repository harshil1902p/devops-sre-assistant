"""Persist Docker lifecycle events (oom, die, start, restart, health) for labelled containers into Postgres.

Why: the Docker daemon keeps only ~256 recent events IN MEMORY. Healthcheck exec events flood that buffer,
so an OOM kill is unrecoverable after ~15 minutes. Kubernetes events also expire (default 1h).
Production equivalent: Azure Monitor Container Insights (KubeEvents table) or a k8s event exporter.

Talks to Docker through a read-only socket proxy (EVENTS=1, CONTAINERS=1, POST=0), never the raw socket.
"""

import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta

import docker
import psycopg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("event-collector")

INTERESTING = ("oom", "die", "kill", "start", "restart", "stop", "health_status", "pause", "unpause")
SCHEMA = """
CREATE TABLE IF NOT EXISTS container_events (
    id           bigserial PRIMARY KEY,
    time         timestamptz NOT NULL,
    time_nano    bigint      NOT NULL,
    service      text        NOT NULL,
    environment  text,
    container    text        NOT NULL,
    action       text        NOT NULL,
    attributes   jsonb       NOT NULL DEFAULT '{}',
    UNIQUE (container, time_nano, action)
);
CREATE INDEX IF NOT EXISTS container_events_service_time ON container_events (service, time DESC);
"""


def run_once(client: docker.DockerClient, conn: psycopg.Connection, backfill_minutes: int) -> None:
    since = datetime.now(UTC) - timedelta(minutes=backfill_minutes)
    stream = client.events(since=since, filters={"type": "container", "label": "sre.service"}, decode=True)
    log.info("streaming events (backfill %s min)", backfill_minutes)
    for e in stream:
        action = e.get("Action", "")
        if not action.startswith(INTERESTING):
            continue
        attrs = e.get("Actor", {}).get("Attributes", {})
        nano = int(e.get("timeNano", 0))
        conn.execute(
            "INSERT INTO container_events (time, time_nano, service, environment, container, action, attributes) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (
                datetime.fromtimestamp(nano / 1e9, UTC), nano, attrs.get("sre.service", "unknown"),
                attrs.get("sre.environment"), attrs.get("name", ""), action,
                json.dumps({k: v for k, v in attrs.items() if k in ("exitCode", "signal", "image")}),
            ),
        )  # fmt: skip
        log.info("%s %s %s", attrs.get("sre.service"), action, attrs.get("exitCode", ""))


def main() -> None:
    client = docker.DockerClient(base_url=os.environ.get("DOCKER_HOST", "tcp://docker-proxy:2375"))
    backoff = 1
    while True:
        try:
            with psycopg.connect(os.environ["EVENTS_DATABASE_URL"], autocommit=True) as conn:
                conn.execute(SCHEMA)
                # backfill whatever the daemon still has; duplicates are ignored by the UNIQUE constraint
                run_once(client, conn, backfill_minutes=60)
        except Exception as e:  # noqa: BLE001 - a collector must survive DB/daemon restarts
            log.warning("collector error: %s; retrying in %ss", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    main()

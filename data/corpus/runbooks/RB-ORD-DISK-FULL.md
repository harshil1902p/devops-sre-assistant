---
doc_id: RB-ORD-DISK-FULL
doc_type: runbook
title: 'Runbook: Disk full on database volume - ''No space left on device'''
scenario: disk-full
service: orders-db
environment: any
language: sql
infrastructure: kubernetes
error_type: DiskFull
error_codes:
- No space left on device
- could not extend file
- ENOSPC
- 'PANIC: could not write to file "pg_wal/xlogtemp"'
severity: SEV1
created: 2025-03-09
tags:
- disk
- postgres
- pvc
- storage
- wal
---

# Runbook: Disk full on database volume - 'No space left on device'

> **Service:** `orders-db` | **Severity:** SEV1 | **Error type:** `DiskFull` | **Infrastructure:** kubernetes

## When To Use This Runbook
Use this runbook when `orders-db` (or a similar sql service) reports any of the error signatures below.

```text
No space left on device
could not extend file
ENOSPC
PANIC: could not write to file "pg_wal/xlogtemp"
```

## Symptoms
- All writes fail; Postgres may crash and refuse to restart.
- PVC usage at 100% in `kubectl exec ... df -h`.

## Example Log Output
```text
ERROR:  could not extend file "base/16384/24576": No space left on device
HINT:  Check free disk space.
PANIC:  could not write to file "pg_wal/xlogtemp.212": No space left on device
LOG:  database system is shut down
```

## Root Cause
WAL files accumulated because an inactive replication slot (from a decommissioned read replica / CDC
connector) prevented WAL recycling, filling the 100 GiB persistent volume.

## Diagnosis Steps
1. Check volume usage: `df -h /var/lib/postgresql/data`.
2. Check WAL directory size and replication slots: `SELECT slot_name, active, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) FROM pg_replication_slots;`.

```bash
kubectl -n orders exec orders-db-0 -- df -h /var/lib/postgresql/data
kubectl -n orders exec orders-db-0 -- du -sh /var/lib/postgresql/data/pg_wal
psql -c "SELECT slot_name, active FROM pg_replication_slots;"
```

## Mitigation (Stop The Bleeding)
- Expand the PVC (StorageClass must have `allowVolumeExpansion: true`): `kubectl patch pvc data-orders-db-0 -p '{"spec":{"resources":{"requests":{"storage":"200Gi"}}}}'`.
- Drop the inactive replication slot: `SELECT pg_drop_replication_slot('old_cdc_slot');`. Never delete WAL files by hand.

## Permanent Fix
- Alert on disk usage > 80% and on inactive replication slots.
- Set `max_slot_wal_keep_size` to bound WAL retention.

```ini
# postgresql.conf
max_slot_wal_keep_size = 20GB
```

## Related Documents
- Post-mortem: PM-INC-2025-0098 (2025-03-02)
- Knowledge base: KB-0007

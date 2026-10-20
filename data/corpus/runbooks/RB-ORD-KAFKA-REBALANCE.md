---
doc_id: RB-ORD-KAFKA-REBALANCE
doc_type: runbook
title: 'Runbook: Kafka consumer group rebalance storm - CommitFailedError'
scenario: kafka-rebalance
service: orders-worker
environment: any
language: python
infrastructure: kubernetes
error_type: CommitFailedError
error_codes:
- 'CommitFailedError: Commit cannot be completed since the group has already rebalanced'
- max.poll.interval.ms
- consumer lag
severity: SEV3
created: 2025-05-13
tags:
- consumer-lag
- kafka
- messaging
- rebalance
---

# Runbook: Kafka consumer group rebalance storm - CommitFailedError

> **Service:** `orders-worker` | **Severity:** SEV3 | **Error type:** `CommitFailedError` | **Infrastructure:** kubernetes

## When To Use This Runbook
Use this runbook when `orders-worker` (or a similar python service) reports any of the error signatures below.

```text
CommitFailedError: Commit cannot be completed since the group has already rebalanced
max.poll.interval.ms
consumer lag
```

## Symptoms
- Consumer lag grows continuously; same messages processed more than once.
- Frequent `rebalancing` logs.

## Example Log Output
```text
kafka.errors.CommitFailedError: CommitFailedError: Commit cannot be completed since the group has already
rebalanced and assigned the partitions to another member. This means that the time between subsequent calls
to poll() was longer than the configured max_poll_interval_ms
WARNING Heartbeat failed for group orders-worker because it is rebalancing
```

## Root Cause
A batch took longer to process than `max.poll.interval.ms` (default 300 s), so the broker evicted the
consumer and reassigned partitions. The slow consumer's commit then failed, the batch was redelivered,
and the cycle repeated.

## Diagnosis Steps
1. Measure per-batch processing time vs `max_poll_interval_ms`.
2. Check lag: `kafka-consumer-groups --describe --group orders-worker`.

```bash
kafka-consumer-groups --bootstrap-server kafka:9092 --describe --group orders-worker
kubectl -n orders logs deploy/orders-worker --since=15m | grep -E 'rebalanc|CommitFailed'
```

## Mitigation (Stop The Bleeding)
- Reduce `max_poll_records` so each batch finishes well within the poll interval.

## Permanent Fix
- Make handlers idempotent (dedupe on message key) because redelivery will happen.
- Move slow I/O out of the poll loop or increase `max_poll_interval_ms` deliberately.

```python
consumer = KafkaConsumer("orders", group_id="orders-worker",
                         max_poll_records=50, max_poll_interval_ms=600_000, enable_auto_commit=False)
```

## Related Documents
- Post-mortem: PM-INC-2025-0175 (2025-05-06)
- Knowledge base: KB-0003

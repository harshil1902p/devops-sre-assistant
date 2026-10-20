---
doc_id: RB-RPT-JVM-HEAP
doc_type: runbook
title: 'Runbook: Java heap space OutOfMemoryError in reporting-service'
scenario: jvm-heap-oom
service: reporting-service
environment: any
language: java
infrastructure: kubernetes
error_type: OutOfMemoryError
error_codes:
- 'java.lang.OutOfMemoryError: Java heap space'
- GC overhead limit exceeded
- -XX:MaxRAMPercentage
severity: SEV3
created: 2025-05-27
tags:
- gc
- heap
- java
- jvm
- memory
---

# Runbook: Java heap space OutOfMemoryError in reporting-service

> **Service:** `reporting-service` | **Severity:** SEV3 | **Error type:** `OutOfMemoryError` | **Infrastructure:** kubernetes

## When To Use This Runbook
Use this runbook when `reporting-service` (or a similar java service) reports any of the error signatures below.

```text
java.lang.OutOfMemoryError: Java heap space
GC overhead limit exceeded
-XX:MaxRAMPercentage
```

## Symptoms
- Java exception in logs (unlike OOMKilled, the JVM itself reports it); container usually keeps running.
- Large report exports fail; GC pauses grow.

## Example Log Output
```text
Exception in thread "report-exec-3" java.lang.OutOfMemoryError: Java heap space
    at java.base/java.util.Arrays.copyOf(Arrays.java:3537)
    at com.contoso.reporting.ExportService.buildCsv(ExportService.java:88)
```

## Root Cause
The JVM max heap is smaller than what the export needs because it loads the whole result set into memory.
The container limit (2Gi) was not the constraint; the heap size (default 25% of RAM) was.

## Diagnosis Steps
1. Distinguish from container OOMKilled: here the JVM throws an exception and logs it; exit code is not 137.
2. Check heap flags: `java -XX:+PrintFlagsFinal -version | grep MaxHeapSize` inside the container.
3. Capture a heap dump with `-XX:+HeapDumpOnOutOfMemoryError`.

```bash
kubectl -n reporting logs deploy/reporting-service --since=1h | grep -B2 -A5 OutOfMemoryError
kubectl -n reporting exec deploy/reporting-service -- java -XX:+PrintFlagsFinal -version | grep -E 'MaxHeapSize|MaxRAMPercentage'
```

## Mitigation (Stop The Bleeding)
- Set `-XX:MaxRAMPercentage=75` so the heap scales with the container limit.

## Permanent Fix
- Stream exports (cursor + chunked CSV writer) instead of building them in memory.

```yaml
env:
  - name: JAVA_TOOL_OPTIONS
    value: "-XX:MaxRAMPercentage=75 -XX:+HeapDumpOnOutOfMemoryError"
```

## Related Documents
- Post-mortem: PM-INC-2025-0193 (2025-05-20)
- Knowledge base: KB-0012

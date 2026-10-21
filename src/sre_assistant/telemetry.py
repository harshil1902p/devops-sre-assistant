"""OpenTelemetry: traces + metrics, exported to Application Insights when configured (no-op otherwise).

Vendor-neutral API in the code (opentelemetry.trace / metrics); the Azure exporter is wired once at startup.
Auth to App Insights is keyless (Entra ID) when the resource has local auth disabled.
"""

import logging

from opentelemetry import metrics, trace

tracer = trace.get_tracer("sre_assistant")
meter = metrics.get_meter("sre_assistant")

llm_tokens = meter.create_counter("sre.llm.tokens", unit="{token}", description="LLM tokens by model and direction")
llm_cost = meter.create_counter("sre.llm.cost", unit="USD", description="Estimated LLM spend (upper bound)")
diagnose_duration = meter.create_histogram("sre.diagnose.duration", unit="s", description="End-to-end diagnosis time")
diagnoses = meter.create_counter("sre.diagnoses", description="Diagnoses by outcome")
tool_calls = meter.create_counter("sre.mcp.tool_calls", description="MCP tool calls by tool and outcome")

_configured = False


def setup_telemetry(connection_string: str | None, service_name: str = "sre-agent-api") -> bool:
    global _configured
    if _configured or not connection_string:
        return _configured
    import os

    from azure.identity import DefaultAzureCredential
    from azure.monitor.opentelemetry import configure_azure_monitor

    os.environ.setdefault("OTEL_SERVICE_NAME", service_name)
    configure_azure_monitor(
        connection_string=connection_string,
        credential=DefaultAzureCredential(exclude_interactive_browser_credential=True),
        logger_name="sre",  # export only our loggers, not every library's INFO noise
        enable_live_metrics=False,
    )
    logging.getLogger("sre").setLevel(logging.INFO)
    _configured = True
    return True


def record_usage(model: str, usage: dict) -> None:
    attrs = {"model": model}
    llm_tokens.add(usage.get("input_tokens", 0), {**attrs, "direction": "input"})
    llm_tokens.add(usage.get("output_tokens", 0), {**attrs, "direction": "output"})
    llm_cost.add(usage.get("cost_usd_upper_bound", 0.0), attrs)

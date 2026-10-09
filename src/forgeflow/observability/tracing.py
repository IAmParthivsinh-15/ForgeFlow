"""LLM tracing to Langfuse over OpenTelemetry (spec sections 68, 142).

Agents SDK spans (agent, generation, tool, handoff) are converted to OpenTelemetry by
the OpenInference instrumentor and exported to Langfuse's OTLP endpoint. ForgeFlow
stamps every span with workflow / task / agent ids, so a Langfuse session is one
workflow (trace hierarchy: workflow -> agent run -> LLM / tool).

Privacy: prompt and response text is not exported unless TRACE_INCLUDE_CONTENT=true.
Credentials are never in prompts or tool arguments by design (spec section 71), and
are never span attributes. The SDK's default exporter to OpenAI is replaced, so
traces go only to the configured Langfuse project.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from opentelemetry.sdk.trace import SpanProcessor

from forgeflow.core.config import Settings
from forgeflow.core.logging import correlation, log_event

logger = logging.getLogger(__name__)
_configured = False


class CorrelationSpanProcessor(SpanProcessor):
    """Adds ForgeFlow ids (from the logging context) to every span as it starts."""

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        fields = correlation()
        workflow_id = fields.get("workflow_id")
        if workflow_id:
            span.set_attribute("session.id", workflow_id)
            span.set_attribute("langfuse.session.id", workflow_id)
            span.set_attribute("forgeflow.workflow_id", workflow_id)
            span.set_attribute("langfuse.trace.metadata.workflow_id", workflow_id)
        for key in ("task_id", "agent"):
            if fields.get(key):
                span.set_attribute(f"forgeflow.{key}", fields[key])
                span.set_attribute(f"langfuse.trace.metadata.{key}", fields[key])


def langfuse_enabled(settings: Settings) -> bool:
    return bool(settings.langfuse_public_key and settings.langfuse_secret_key)


def setup_tracing(settings: Settings, service: str, exporter: Any = None) -> bool:
    """Install the Langfuse exporter once per process. Returns True when tracing is on.

    `exporter` lets tests capture spans in memory instead of sending them.
    """
    global _configured
    if _configured:
        return True
    if exporter is None and not langfuse_enabled(settings):
        return False
    from openinference.instrumentation import TraceConfig
    from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": f"forgeflow-{service}"}))
    provider.add_span_processor(CorrelationSpanProcessor())
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        auth = base64.b64encode(
            f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
        ).decode()
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=f"{settings.langfuse_host.rstrip('/')}/api/public/otel/v1/traces",
                    headers={"Authorization": f"Basic {auth}"},
                )
            )
        )
    hide = not settings.trace_include_content
    OpenAIAgentsInstrumentor().instrument(
        tracer_provider=provider,
        exclusive_processor=True,  # replaces the SDK's default export to OpenAI
        config=TraceConfig(hide_inputs=hide, hide_outputs=hide),
    )
    from agents import set_tracing_disabled

    set_tracing_disabled(False)
    _configured = True
    log_event(
        logger,
        "LLM tracing enabled",
        target="memory" if exporter is not None else settings.langfuse_host,
        include_content=settings.trace_include_content,
    )
    return True

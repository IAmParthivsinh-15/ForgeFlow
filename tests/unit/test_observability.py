"""Metrics from events, the /metrics endpoint, and Langfuse-style tracing correlation."""

import httpx
from prometheus_client import REGISTRY

from forgeflow.apps.api.main import create_app
from forgeflow.observability.metrics import count_worktrees, observe_event
from forgeflow.platform.events.kafka import OutboxRelay
from forgeflow.platform.state.store import Commit
from forgeflow.schemas.events import Event, EventType, Topics
from tests.conftest import drive


def value(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_events_drive_the_spec_metrics():
    before = value("forgeflow_tasks_total", kind="qa", status="completed")
    observe_event(
        Event(
            event_type=EventType.TASK_COMPLETED,
            topic=Topics.TASK,
            workflow_id="wf_m",
            payload={"kind": "qa", "duration_ms": 1500},
        )
    )
    assert value("forgeflow_tasks_total", kind="qa", status="completed") == before + 1
    assert value("forgeflow_task_duration_seconds_count", kind="qa") >= 1

    fails = value("forgeflow_ci_failures_total", provider="jenkins", result="FAILURE")
    observe_event(
        Event(
            event_type=EventType.CI_BUILD_COMPLETED,
            topic=Topics.TASK,
            workflow_id="wf_m",
            payload={"provider": "jenkins", "result": "FAILURE", "build_duration_ms": 90_000},
        )
    )
    assert value("forgeflow_ci_failures_total", provider="jenkins", result="FAILURE") == fails + 1

    tools = value("forgeflow_tool_calls_total", agent="qa", tool="read_file")
    observe_event(
        Event(
            event_type=EventType.AGENT_RUN_COMPLETED,
            topic=Topics.AGENT,
            workflow_id="wf_m",
            payload={"agent_type": "qa", "duration_ms": 2000, "tools": ["read_file", "read_file"]},
        )
    )
    assert value("forgeflow_tool_calls_total", agent="qa", tool="read_file") == tools + 2

    done = value("forgeflow_workflows_total", status="COMPLETED")
    observe_event(
        Event(
            event_type=EventType.WORKFLOW_STATUS_CHANGED,
            workflow_id="wf_m",
            payload={"previous": "TESTING", "current": "COMPLETED", "age_seconds": 42.0},
        )
    )
    assert value("forgeflow_workflows_total", status="COMPLETED") == done + 1
    # Malformed payloads never raise.
    observe_event(
        Event(event_type=EventType.TASK_COMPLETED, workflow_id="wf_m", payload={"duration_ms": "x"})
    )


async def test_relay_feeds_metrics_with_real_workflow_events(container, git_repo):
    """Run a workflow, then relay its outbox through a fake producer into the observer."""
    seen: list[Event] = []

    class Producer:
        async def send_and_wait(self, topic, value, key):
            return None

    relay = OutboxRelay(container.store, "unused:9092", observer=seen.append)
    relay.producer = Producer()  # type: ignore[assignment]
    svc = container.service
    wf = await svc.create_workflow("Run tests and QA", "app")
    await svc.process_analysis(wf.workflow_id, 1)
    await container.execution.start_execution(wf.workflow_id)
    await drive(container, wf.workflow_id)
    while await relay.relay_once():
        pass
    types = {e.event_type for e in seen}
    assert {
        EventType.TASK_COMPLETED,
        EventType.CHECK_COMPLETED,
        EventType.AGENT_RUN_COMPLETED,
    } <= types
    completed = [e for e in seen if e.event_type == EventType.TASK_COMPLETED]
    assert all(
        "kind" in e.payload and isinstance(e.payload.get("duration_ms"), int) for e in completed
    )
    status = [e for e in seen if e.event_type == EventType.WORKFLOW_STATUS_CHANGED]
    assert all("age_seconds" in e.payload for e in status)
    assert not await container.store.fetch_unpublished_events()


async def test_metrics_endpoint_and_api_latency(container):
    async def factory():
        return container

    app = create_app(factory)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/health/live")).status_code == 200
            body = (await client.get("/metrics")).text
    assert (
        'forgeflow_api_latency_seconds_count{method="GET",route="/health/live",status="200"}'
        in body
    )
    assert "forgeflow_workflows_total" in body and "forgeflow_kafka_consumer_lag" in body


def test_worktree_count(tmp_path):
    (tmp_path / "wf_a" / "T2-backend").mkdir(parents=True)
    (tmp_path / "wf_a" / "integration").mkdir()
    (tmp_path / "wf_b" / "T1").mkdir(parents=True)
    (tmp_path / "README.md").write_text("x")
    assert count_worktrees(tmp_path) == 3
    assert count_worktrees(tmp_path / "missing") == 0


async def test_traces_carry_workflow_ids_and_no_content_by_default(settings):
    from agents import Agent, ModelResponse, Runner, Usage, set_tracing_disabled
    from agents.models.interface import Model
    from openai.types.responses import ResponseOutputMessage, ResponseOutputText
    from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from forgeflow.agents.context import AgentRuntimeContext
    from forgeflow.core.logging import bind_context, clear_context
    from forgeflow.models.executor import AgentExecutor
    from forgeflow.observability import tracing

    class FakeModel(Model):
        async def get_response(self, *args, **kwargs):
            message = ResponseOutputMessage(
                id="m1",
                type="message",
                role="assistant",
                status="completed",
                content=[
                    ResponseOutputText(type="output_text", text="SECRET-ANSWER", annotations=[])
                ],
            )
            return ModelResponse(output=[message], usage=Usage(), response_id=None)

        def stream_response(self, *args, **kwargs):
            raise NotImplementedError

    exporter = InMemorySpanExporter()
    try:
        assert tracing.setup_tracing(settings, "test", exporter=exporter)
        executor = AgentExecutor(registry=None, include_content=False)  # type: ignore[arg-type]
        ctx = AgentRuntimeContext(workflow_id="wf_trace", task_id="wf_trace.V1-qa")
        agent = Agent(name="QA", instructions="SECRET-PROMPT", model=FakeModel())
        bind_context(workflow_id="wf_trace", task_id="wf_trace.V1-qa")
        await Runner.run(
            agent, "SECRET-INPUT", context=ctx, run_config=executor._run_config(agent, ctx)
        )
        spans = exporter.get_finished_spans()
        assert spans
        assert all(s.attributes.get("forgeflow.workflow_id") == "wf_trace" for s in spans)
        assert any(s.attributes.get("session.id") == "wf_trace" for s in spans)
        assert any(s.attributes.get("forgeflow.agent") == "QA" for s in spans)
        dump = " ".join(str(dict(s.attributes)) for s in spans)
        assert "SECRET-INPUT" not in dump and "SECRET-ANSWER" not in dump
    finally:
        clear_context()
        OpenAIAgentsInstrumentor().uninstrument()
        set_tracing_disabled(True)
        tracing._configured = False


async def test_publishing_nothing_when_store_has_no_events(store):
    relay = OutboxRelay(store, "unused:9092", observer=lambda e: None)

    class Producer:
        async def send_and_wait(self, *a, **k):
            raise AssertionError("nothing to send")

    relay.producer = Producer()  # type: ignore[assignment]
    await store.commit(Commit(events=[]))
    assert await relay.relay_once() == 0

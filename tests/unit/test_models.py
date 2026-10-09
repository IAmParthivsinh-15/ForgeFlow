from pathlib import Path

import httpx
import openai
import pytest

from forgeflow.core.errors import AllProvidersFailed
from forgeflow.models import executor as executor_module
from forgeflow.models.config import ModelRegistry
from forgeflow.models.executor import AgentExecutor, InvalidStructuredOutput, _parse, extract_json
from forgeflow.schemas.workflow import IntakeAssessment

MODELS_YAML = Path(__file__).parents[2] / "config" / "models.yaml"


def registry(env):
    return ModelRegistry.from_path(MODELS_YAML, env)


def test_chain_contains_only_configured_providers_in_priority_order():
    env = {
        "NVIDIA_API_KEY": "nv-key",
        "NVIDIA_MODEL_PLANNING": "nv-model",
        "OPENAI_API_KEY": "oa-key",
        "OPENAI_MODEL_PLANNING": "oa-model",
        "GROQ_MODEL_PLANNING": "groq-model",  # no GROQ_API_KEY -> skipped
        "OLLAMA_MODEL": "llama",  # OLLAMA_ENABLED unset -> skipped
    }
    chain = registry(env).chain("planning")
    assert [(m.provider, m.model) for m in chain] == [
        ("nvidia", "nv-model"),
        ("openai", "oa-model"),
    ]


def test_base_url_override_and_keyless_local_provider():
    env = {
        "OPENAI_API_KEY": "k",
        "OPENAI_MODEL_FAST": "m",
        "OPENAI_BASE_URL": "http://litellm:4000/v1",
        "OLLAMA_ENABLED": "true",
        "OLLAMA_MODEL": "llama",
    }
    chain = registry(env).chain("fast")
    assert chain[0].base_url == "http://litellm:4000/v1"
    assert chain[1].provider == "ollama" and chain[1].api_key == "not-required"


def test_api_key_never_appears_in_repr_or_describe():
    env = {"NVIDIA_API_KEY": "super-secret", "NVIDIA_MODEL_FAST": "m"}
    reg = registry(env)
    assert "super-secret" not in repr(reg.chain("fast"))
    assert "super-secret" not in str(reg.describe())


def test_extract_json_handles_fences_and_prose():
    assert extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert extract_json('Here you go: {"a": {"b": 2}} done') == '{"a": {"b": 2}}'
    with pytest.raises(InvalidStructuredOutput):
        extract_json("no json here")


def test_parse_reports_validation_errors():
    with pytest.raises(InvalidStructuredOutput, match="intent"):
        _parse(IntakeAssessment, '{"summary": "x"}')


def _api_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=httpx.Request("POST", "http://x"))


async def test_executor_falls_back_and_records_attempts(monkeypatch):
    env = {
        "NVIDIA_API_KEY": "a",
        "NVIDIA_MODEL_FAST": "nv",
        "OPENAI_API_KEY": "b",
        "OPENAI_MODEL_FAST": "oa",
    }
    calls = []

    async def fake_run_once(
        self, resolved, build_agent, input_text, output_type, context, usage=None
    ):
        calls.append(resolved.provider)
        if resolved.provider == "nvidia":
            raise _api_error()
        return IntakeAssessment(intent="feature", summary="ok")

    monkeypatch.setattr(executor_module.AgentExecutor, "_run_once", fake_run_once)
    run = await AgentExecutor(registry(env)).run_structured(
        profile="fast", build_agent=None, input_text="x", output_type=IntakeAssessment
    )
    assert calls == ["nvidia", "openai"]
    assert run.output.summary == "ok"
    assert [(a.provider, a.status) for a in run.attempts] == [
        ("nvidia", "failed"),
        ("openai", "succeeded"),
    ]
    assert "APIConnectionError" in run.attempts[0].fallback_reason


async def test_executor_raises_when_all_providers_fail(monkeypatch):
    async def always_fail(self, *args, **kwargs):
        raise _api_error()

    monkeypatch.setattr(executor_module.AgentExecutor, "_run_once", always_fail)
    env = {"NVIDIA_API_KEY": "a", "NVIDIA_MODEL_FAST": "nv"}
    with pytest.raises(AllProvidersFailed) as info:
        await AgentExecutor(registry(env)).run_structured(
            profile="fast", build_agent=None, input_text="x", output_type=IntakeAssessment
        )
    assert len(info.value.attempts) == 1


async def test_executor_explains_missing_configuration():
    with pytest.raises(AllProvidersFailed, match="NVIDIA_API_KEY"):
        await AgentExecutor(registry({})).run_structured(
            profile="fast", build_agent=None, input_text="x", output_type=IntakeAssessment
        )

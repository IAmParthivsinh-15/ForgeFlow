"""Runs an Agents SDK agent against a profile's provider fallback chain.

Handles the two structured-output modes:
- json_schema: the SDK enforces `output_type` through the provider.
- prompt: the JSON schema is appended to the instructions, the reply is
  validated with Pydantic, and the model is asked to repair invalid output.

Every provider attempt is recorded (provider, model, latency, fallback reason).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import openai
from agents import (
    Agent,
    AgentOutputSchema,
    MaxTurnsExceeded,
    Model,
    ModelBehaviorError,
    OpenAIChatCompletionsModel,
    RunConfig,
    Runner,
)
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from forgeflow.core.errors import AllProvidersFailed
from forgeflow.core.logging import bind_context, log_event
from forgeflow.models.config import ModelRegistry, ResolvedModel
from forgeflow.schemas.workflow import ProviderAttempt

logger = logging.getLogger(__name__)

# build_agent(model, output_type_or_none, extra_instructions) -> Agent
AgentBuilder = Callable[[Model, AgentOutputSchema | None, str], Agent[Any]]


class InvalidStructuredOutput(Exception):
    pass


@dataclass
class StructuredRun[T: BaseModel]:
    output: T
    attempts: list[ProviderAttempt] = field(default_factory=list)


def extract_json(text: str) -> str:
    """Pull a JSON object out of a model reply that may include fences or prose."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0]
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end <= start:
        raise InvalidStructuredOutput("reply did not contain a JSON object")
    return stripped[start : end + 1]


def schema_instructions(output_type: type[BaseModel]) -> str:
    schema = json.dumps(output_type.model_json_schema(), indent=None)
    return (
        "\n\n# Response Format\n"
        "When you have finished using tools, reply with ONLY a single JSON object that "
        "validates against this JSON Schema. No prose, no markdown fences.\n"
        f"{schema}"
    )


def _parse[T: BaseModel](output_type: type[T], raw: Any) -> T:
    if isinstance(raw, output_type):
        return raw
    if isinstance(raw, BaseModel):
        raw = raw.model_dump()
    try:
        if isinstance(raw, str):
            return output_type.model_validate_json(extract_json(raw))
        return output_type.model_validate(raw)
    except ValidationError as exc:
        raise InvalidStructuredOutput(_summarise_validation(exc)) from exc


def _summarise_validation(exc: ValidationError) -> str:
    parts = [f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()]
    return "; ".join(parts[:10])


def with_capabilities(agent: Agent[Any], context: Any) -> Agent[Any]:
    """Runtime agent = base agent + resolved skills + MCP proxy tools (spec section 225)."""
    prompt = getattr(context, "skills_prompt", "") or ""
    extra = list(getattr(context, "extra_tools", None) or [])
    if not prompt and not extra:
        return agent
    instructions = agent.instructions if isinstance(agent.instructions, str) else ""
    # Keep the output contract last: insert skills before the response-format section.
    marker = "\n\n# Response Format\n"
    if prompt and marker in instructions:
        head, tail = instructions.split(marker, 1)
        instructions = head + prompt + marker + tail
    else:
        instructions += prompt
    return agent.clone(instructions=instructions, tools=[*agent.tools, *extra])


class AgentExecutor:
    def __init__(
        self,
        registry: ModelRegistry,
        max_turns: int = 20,
        repair_attempts: int = 2,
        include_content: bool = False,
    ):
        self.registry = registry
        self.max_turns = max_turns
        self.repair_attempts = repair_attempts
        # Whether traces may contain prompt/response text (spec section 68).
        self.include_content = include_content

    def _run_config(self, agent: Agent[Any], context: Any) -> RunConfig:
        """Correlate SDK traces with ForgeFlow ids (spec section 142)."""
        workflow_id = getattr(context, "workflow_id", None)
        task_id = getattr(context, "task_id", None)
        bind_context(agent=agent.name)
        metadata = {k: v for k, v in {"workflow_id": workflow_id, "task_id": task_id}.items() if v}
        return RunConfig(
            workflow_name=f"forgeflow {agent.name}",
            group_id=workflow_id,
            trace_metadata={**metadata, "agent": agent.name},
            trace_include_sensitive_data=self.include_content,
        )

    def _model(self, resolved: ResolvedModel) -> Model:
        client = AsyncOpenAI(
            base_url=resolved.base_url,
            api_key=resolved.api_key,
            timeout=resolved.timeout_seconds,
            max_retries=1,
        )
        return OpenAIChatCompletionsModel(model=resolved.model, openai_client=client)

    async def run_structured[T: BaseModel](
        self,
        *,
        profile: str,
        build_agent: AgentBuilder,
        input_text: str,
        output_type: type[T],
        context: Any = None,
    ) -> StructuredRun[T]:
        chain = self.registry.chain(profile)
        if not chain:
            raise AllProvidersFailed(
                f"no model provider is configured for profile '{profile}'. Set an API key "
                "and model id in .env (e.g. NVIDIA_API_KEY and NVIDIA_MODEL_"
                f"{profile.upper()}), or set FAKE_LLM=true for offline runs."
            )
        attempts: list[ProviderAttempt] = []
        for resolved in chain:
            started = time.perf_counter()
            usage: dict[str, int] = {}
            try:
                output = await self._run_once(
                    resolved, build_agent, input_text, output_type, context, usage
                )
            except (
                openai.APIError,
                ModelBehaviorError,
                MaxTurnsExceeded,
                InvalidStructuredOutput,
                TimeoutError,
            ) as exc:
                reason = f"{type(exc).__name__}: {str(exc)[:300]}"
                attempts.append(
                    ProviderAttempt(
                        provider=resolved.provider,
                        model=resolved.model,
                        status="failed",
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        fallback_reason=reason,
                    )
                )
                log_event(
                    logger,
                    "model provider attempt failed",
                    logging.WARNING,
                    provider=resolved.provider,
                    model=resolved.model,
                    profile=profile,
                    fallback_reason=reason,
                )
                continue
            attempts.append(
                ProviderAttempt(
                    provider=resolved.provider,
                    model=resolved.model,
                    status="succeeded",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    input_tokens=usage.get("input", 0),
                    output_tokens=usage.get("output", 0),
                )
            )
            return StructuredRun(output=output, attempts=attempts)
        raise AllProvidersFailed(
            f"all {len(chain)} configured provider(s) failed for profile '{profile}'", attempts
        )

    async def _run_once[T: BaseModel](
        self,
        resolved: ResolvedModel,
        build_agent: AgentBuilder,
        input_text: str,
        output_type: type[T],
        context: Any,
        usage: dict[str, int] | None = None,
    ) -> T:
        usage = usage if usage is not None else {}

        def count(result: Any) -> None:
            total = getattr(getattr(result, "context_wrapper", None), "usage", None)
            if total is not None:
                usage["input"] = usage.get("input", 0) + int(getattr(total, "input_tokens", 0) or 0)
                usage["output"] = usage.get("output", 0) + int(
                    getattr(total, "output_tokens", 0) or 0
                )

        model = self._model(resolved)
        if resolved.structured_output == "json_schema":
            agent = with_capabilities(
                build_agent(model, AgentOutputSchema(output_type, strict_json_schema=False), ""),
                context,
            )
            result = await Runner.run(
                agent,
                input_text,
                context=context,
                max_turns=self.max_turns,
                run_config=self._run_config(agent, context),
            )
            count(result)
            return _parse(output_type, result.final_output)

        agent = with_capabilities(
            build_agent(model, None, schema_instructions(output_type)), context
        )
        run_config = self._run_config(agent, context)
        result = await Runner.run(
            agent, input_text, context=context, max_turns=self.max_turns, run_config=run_config
        )
        count(result)
        for attempt in range(self.repair_attempts + 1):
            try:
                return _parse(output_type, str(result.final_output or ""))
            except InvalidStructuredOutput as exc:
                if attempt == self.repair_attempts:
                    raise
                repair = (
                    f"Your previous reply was not valid: {exc}. Reply again with ONLY the "
                    "corrected JSON object that satisfies the schema."
                )
                history = result.to_input_list() + [{"role": "user", "content": repair}]
                result = await Runner.run(
                    agent, history, context=context, max_turns=self.max_turns, run_config=run_config
                )
                count(result)
        raise InvalidStructuredOutput("unreachable")

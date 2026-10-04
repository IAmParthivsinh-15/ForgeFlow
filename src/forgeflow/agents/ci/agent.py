from __future__ import annotations

from pathlib import Path

from agents import Agent, AgentOutputSchema, Model

from forgeflow.agents.ci.tools import get_tools
from forgeflow.agents.context import AgentRuntimeContext
from forgeflow.core.prompts import Prompt, load_prompt

PROMPT_PATH = Path(__file__).with_name("prompt.md")
MODEL_PROFILE = "fast"


def prompt() -> Prompt:
    return load_prompt(PROMPT_PATH)


def create_ci_agent(
    model: Model,
    output_type: AgentOutputSchema | None = None,
    extra_instructions: str = "",
) -> Agent[AgentRuntimeContext]:
    return Agent[AgentRuntimeContext](
        name="CI",
        instructions=prompt().text + extra_instructions,
        model=model,
        tools=list(get_tools()),
        output_type=output_type,
    )

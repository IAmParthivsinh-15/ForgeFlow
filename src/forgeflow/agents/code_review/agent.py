from __future__ import annotations

from pathlib import Path

from agents import Agent, AgentOutputSchema, Model

from forgeflow.agents.code_review.tools import get_tools
from forgeflow.agents.context import AgentRuntimeContext
from forgeflow.core.prompts import Prompt, load_prompt

PROMPT_PATH = Path(__file__).with_name("prompt.md")
MODEL_PROFILE = "review"


def prompt() -> Prompt:
    return load_prompt(PROMPT_PATH)


def create_code_review_agent(
    model: Model,
    output_type: AgentOutputSchema | None = None,
    extra_instructions: str = "",
) -> Agent[AgentRuntimeContext]:
    return Agent[AgentRuntimeContext](
        name="Code Review",
        instructions=prompt().text + extra_instructions,
        model=model,
        tools=list(get_tools()),
        output_type=output_type,
    )

from __future__ import annotations

from pathlib import Path

from agents import Agent, AgentOutputSchema, Model

from forgeflow.agents.context import AgentRuntimeContext
from forgeflow.agents.requirement_analyzer.tools import get_tools
from forgeflow.core.prompts import Prompt, load_prompt

PROMPT_PATH = Path(__file__).with_name("prompt.md")
MODEL_PROFILE = "planning"


def prompt() -> Prompt:
    return load_prompt(PROMPT_PATH)


def create_requirement_analyzer_agent(
    model: Model,
    output_type: AgentOutputSchema | None = None,
    extra_instructions: str = "",
    with_repository_tools: bool = True,
) -> Agent[AgentRuntimeContext]:
    return Agent[AgentRuntimeContext](
        name="Requirement Analyzer",
        instructions=prompt().text + extra_instructions,
        model=model,
        tools=list(get_tools()) if with_repository_tools else [],
        output_type=output_type,
    )

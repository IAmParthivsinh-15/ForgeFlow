from agents import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from forgeflow.agents.orchestrator import agent as orchestrator
from forgeflow.agents.requirement_analyzer import agent as analyzer
from forgeflow.platform.orchestration.gateway import AnalysisRequest, render_analysis_input
from forgeflow.schemas.workflow import IntakeAssessment


def model():
    client = AsyncOpenAI(base_url="http://localhost:1/v1", api_key="test")
    return OpenAIChatCompletionsModel(model="test-model", openai_client=client)


def test_prompts_are_versioned():
    assert orchestrator.prompt().version == "1.0.0"
    assert analyzer.prompt().version == "1.0.0"


def test_requirement_analyzer_has_only_read_only_tools():
    agent = analyzer.create_requirement_analyzer_agent(model())
    assert sorted(t.name for t in agent.tools) == ["list_files", "read_file", "search_code"]
    no_repo = analyzer.create_requirement_analyzer_agent(model(), with_repository_tools=False)
    assert no_repo.tools == []


def test_orchestrator_has_no_tools_and_includes_schema_instructions():
    agent = orchestrator.create_orchestrator_agent(model(), extra_instructions="\nSCHEMA")
    assert agent.tools == []
    assert agent.instructions.endswith("SCHEMA")


def test_analysis_input_includes_answers_and_finalize_instruction():
    text = render_analysis_input(
        AnalysisRequest(
            request="Add OAuth",
            intake=IntakeAssessment(intent="feature", summary="Add OAuth login"),
            repository_attached=False,
            previous_specification=None,
            answered_questions=[],
            must_finalize=True,
            max_questions=3,
        )
    )
    assert "Add OAuth" in text
    assert "No repository is attached" in text
    assert "You MUST finalize now" in text


def test_development_agents_have_the_right_tools():
    from forgeflow.agents.developer import agent as developer
    from forgeflow.agents.developer_subagent import agent as subagent
    from forgeflow.agents.integrator import agent as integrator

    names = lambda a: sorted(t.name for t in a.tools)  # noqa: E731
    assert names(developer.create_developer_agent(model())) == [
        "list_files",
        "read_file",
        "search_code",
    ]
    assert names(subagent.create_developer_subagent(model())) == [
        "delete_file",
        "list_files",
        "read_file",
        "replace_in_file",
        "run_check",
        "search_code",
        "write_file",
    ]
    assert "run_check" not in names(subagent.create_developer_subagent(model(), with_checks=False))
    assert names(integrator.create_integrator_agent(model())) == [
        "list_files",
        "read_file",
        "search_code",
        "write_file",
    ]
    assert developer.prompt().version == "1.1.0"  # adds A2A answering
    for module in (subagent, integrator):
        assert module.prompt().version == "1.0.0"

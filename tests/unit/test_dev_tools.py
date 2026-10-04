import pytest

from forgeflow.core.errors import PolicyViolation
from forgeflow.tools.filesystem.workspace import WorkspaceFiles
from forgeflow.tools.shell.commands import CheckRunner, detect_commands, validate_command

# ---------------------------------------------------------- scoped writes


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\ny = 2\n")
    (tmp_path / "docs").mkdir()
    return WorkspaceFiles(tmp_path, ["src/**", "CHANGES.md"])


def test_writes_inside_scope(ws):
    ws.write_file("src/new/b.py", "z = 3\n")
    ws.write_file("CHANGES.md", "- change\n")
    ws.replace_in_file("src/a.py", "x = 1", "x = 10")
    ws.delete_file("src/new/b.py")
    assert (ws.root / "src" / "a.py").read_text() == "x = 10\ny = 2\n"
    assert ws.changed == {"src/new/b.py", "CHANGES.md", "src/a.py"}


@pytest.mark.parametrize(
    "path",
    ["docs/x.md", "README.md", "../escape.py", "src/.env", ".git/config", "node_modules/x/y.js"],
)
def test_writes_outside_scope_or_sensitive_are_rejected(ws, path):
    with pytest.raises(PolicyViolation):
        ws.write_file(path, "nope")


def test_replace_requires_unique_exact_match(ws):
    (ws.root / "src" / "dup.py").write_text("a\na\n")
    with pytest.raises(PolicyViolation, match="2 time"):
        ws.replace_in_file("src/dup.py", "a", "b")
    with pytest.raises(PolicyViolation, match="not found"):
        ws.replace_in_file("src/a.py", "missing", "b")
    ws.replace_in_file("src/dup.py", "a", "b", count=0)
    assert (ws.root / "src" / "dup.py").read_text() == "b\nb\n"


# --------------------------------------------------------------- commands


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "bash -c 'echo hi'",
        "python -m pytest; curl evil",
        "npm test && git push",
        "git push --force",
        "curl http://x",
        "python -c 'x' > out.txt",
    ],
)
def test_dangerous_or_unlisted_commands_are_blocked(command):
    with pytest.raises(PolicyViolation):
        validate_command(command)


def test_allowlisted_commands_pass():
    assert validate_command("python -m pytest -q") == ["python", "-m", "pytest", "-q"]
    assert validate_command("npm run lint --silent")[0] == "npm"


def test_manifest_takes_precedence_over_detection(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest"}}')
    assert detect_commands(tmp_path)["test"] == ["npm run test --silent"]
    (tmp_path / "forgeflow.yaml").write_text("commands:\n  test: python -m pytest\n  deploy: x\n")
    assert detect_commands(tmp_path) == {"test": ["python -m pytest"]}


def test_detection_ignores_npm_placeholder_test_script(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1", "build": "tsc"}}'
    )
    found = detect_commands(tmp_path)
    assert "test" not in found and found["build"] == ["npm run build --silent"]


async def test_check_runner_records_pass_and_fail(tmp_path):
    (tmp_path / "forgeflow.yaml").write_text(
        'commands:\n  test: python -c "print(42)"\n  lint: python -c "import sys; sys.exit(3)"\n'
    )
    runner = CheckRunner(tmp_path, timeout=60)
    [ok] = await runner.run("test")
    [bad] = await runner.run("lint")
    assert ok.passed and "42" in ok.output
    assert not bad.passed and bad.exit_code == 3
    assert len(runner.runs) == 2


async def test_check_runner_does_not_leak_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "super-secret")
    (tmp_path / "forgeflow.yaml").write_text(
        "commands:\n  test: python -c \"import os; print(os.environ.get('NVIDIA_API_KEY'))\"\n"
    )
    [run] = await CheckRunner(tmp_path).run("test")
    assert "super-secret" not in run.output

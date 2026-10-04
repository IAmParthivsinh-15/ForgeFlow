import pytest

from forgeflow.core.errors import PolicyViolation
from forgeflow.tools.filesystem.repository import RepositorySandbox


@pytest.fixture
def sandbox(repos_root):
    (repos_root / "demo-app" / "node_modules" / "pkg").mkdir(parents=True)
    (repos_root / "demo-app" / "node_modules" / "pkg" / "index.js").write_text("x")
    (repos_root / "demo-app" / ".env.example").write_text("SECRET=\n")
    return RepositorySandbox(repos_root / "demo-app")


def test_list_files_skips_secrets_and_ignored_dirs(sandbox):
    files = sandbox.list_files(".")
    assert "backend/auth.py" in files
    assert "README.md" in files
    assert ".env.example" in files
    assert ".env" not in files
    assert not any(f.startswith("node_modules") for f in files)


def test_read_file_returns_numbered_lines(sandbox):
    content = sandbox.read_file("backend/auth.py")
    assert content.startswith("1: def login")
    assert "[lines 1-2 of 2]" in content


@pytest.mark.parametrize("path", ["../outside.txt", "../../etc/passwd", "backend/../../x"])
def test_path_escape_is_rejected(sandbox, path):
    with pytest.raises(PolicyViolation):
        sandbox.read_file(path)


def test_secret_files_cannot_be_read(sandbox):
    with pytest.raises(PolicyViolation):
        sandbox.read_file(".env")


def test_ignored_directories_cannot_be_read(sandbox):
    with pytest.raises(PolicyViolation):
        sandbox.read_file("node_modules/pkg/index.js")


def test_search_code_finds_matches_and_never_searches_secrets(sandbox):
    matches = sandbox.search_code("login")
    assert [(m.path, m.line) for m in matches] == [("backend/auth.py", 1)]
    assert sandbox.search_code("do-not-read") == []


def test_search_code_glob_and_invalid_regex(sandbox):
    assert sandbox.search_code("demo", glob="*.py") == []
    with pytest.raises(PolicyViolation):
        sandbox.search_code("(", is_regex=True)

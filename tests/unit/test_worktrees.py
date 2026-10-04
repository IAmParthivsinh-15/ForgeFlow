import pytest

from forgeflow.core.errors import ValidationFailed
from forgeflow.platform.worktrees.manager import WorktreeManager
from forgeflow.tools.git.client import GitClient
from tests.conftest import git


@pytest.fixture
def manager(repos_root, tmp_path):
    return WorktreeManager(GitClient(), repos_root, tmp_path / "workspaces")


async def make(manager, key, base, merges=None):
    return await manager.create(
        workflow_id="wf_test",
        task_id=f"wf_test.{key}",
        key=key,
        repository_path="app",
        base_commit=base,
        merge_commits=merges,
    )


async def test_worktrees_are_isolated_with_conflicting_edits(manager, git_repo):
    """Spec section 92: T1 -> worktree 1, T2 -> worktree 2, conflicting changes stay isolated."""
    base = git(git_repo, "rev-parse", "HEAD")
    w1 = await make(manager, "T1", base)
    w2 = await make(manager, "T2", base)
    assert w1.path != w2.path
    assert w1.workspace.branch == "forgeflow/wf_test/T1"

    (w1.path / "src" / "app.py").write_text("def handler():\n    return 'one'\n")
    (w2.path / "src" / "app.py").write_text("def handler():\n    return 'two'\n")
    c1 = await manager.git.commit_all(w1.path, "t1")
    c2 = await manager.git.commit_all(w2.path, "t2")

    assert "'one'" in (w1.path / "src" / "app.py").read_text()
    assert "'two'" in (w2.path / "src" / "app.py").read_text()
    # The user's checkout and branch are untouched.
    assert "'ok'" in (git_repo / "src" / "app.py").read_text()
    assert git(git_repo, "rev-parse", "HEAD") == base
    assert git(git_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"

    # The two commits genuinely conflict when merged.
    w3 = await make(manager, "T3", base, merges=[("T1", c1), ("T2", c2)])
    assert w3.merged == ["T1"]
    assert "could not pre-merge T2" in w3.warnings[0]


async def test_dependent_worktree_contains_predecessor_code(manager, git_repo):
    base = git(git_repo, "rev-parse", "HEAD")
    w1 = await make(manager, "T1", base)
    (w1.path / "api.py").write_text("API = 1\n")
    c1 = await manager.git.commit_all(w1.path, "api")
    w2 = await make(manager, "T2", base, merges=[("T1", c1)])
    assert (w2.path / "api.py").read_text() == "API = 1\n"


async def test_recreating_a_worktree_starts_clean(manager, git_repo):
    base = git(git_repo, "rev-parse", "HEAD")
    first = await make(manager, "T1", base)
    (first.path / "junk.txt").write_text("x")
    await manager.git.commit_all(first.path, "junk")
    again = await make(manager, "T1", base)
    assert not (again.path / "junk.txt").exists()
    assert git(again.path, "rev-parse", "HEAD") == base


async def test_remove_deletes_worktree_and_optionally_branch(manager, git_repo):
    base = git(git_repo, "rev-parse", "HEAD")
    prepared = await make(manager, "T1", base)
    await manager.remove(prepared.workspace, delete_branch=True)
    assert not prepared.path.exists()
    assert not await manager.git.branch_exists(git_repo, prepared.workspace.branch)


async def test_invalid_names_are_rejected(manager, git_repo):
    with pytest.raises(ValidationFailed):
        await make(manager, "../evil", "abc1234")
    with pytest.raises(ValidationFailed):
        manager.repository("../outside")


async def test_nested_folder_is_not_mistaken_for_a_repository(git_repo):
    nested = git_repo / "src"
    client = GitClient()
    assert await client.is_repository(git_repo)
    assert not await client.is_repository(nested)


async def test_changed_files_and_discard(git_repo):
    client = GitClient()
    (git_repo / "README.md").write_text("changed\n")
    (git_repo / "new.txt").write_text("new\n")
    assert await client.changed_files(git_repo) == ["README.md", "new.txt"]
    await client.discard_paths(git_repo, ["README.md", "new.txt"])
    assert await client.changed_files(git_repo) == []

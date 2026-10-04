"""Task, workspace and diff endpoints (spec sections 61, 64, 65)."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from forgeflow.apps.api.deps import ContainerDep
from forgeflow.core.errors import NotFoundError
from forgeflow.schemas.task import Task, Workspace

router = APIRouter(prefix="/api/v1", tags=["tasks"])


@router.get("/workflows/{workflow_id}/tasks")
async def list_tasks(workflow_id: str, c: ContainerDep) -> list[Task]:
    await c.store.get_workflow(workflow_id)
    return await c.store.list_tasks(workflow_id)


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, c: ContainerDep) -> Task:
    return await c.store.get_task(task_id)


@router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str, c: ContainerDep) -> Task:
    return await c.execution.retry_task(task_id)


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, c: ContainerDep) -> Task:
    return await c.execution.cancel_task(task_id)


@router.get("/tasks/{task_id}/diff", response_class=PlainTextResponse)
async def task_diff(task_id: str, c: ContainerDep) -> str:
    """The task's own commit (implement) or the whole integration (integrate)."""
    task = await c.store.get_task(task_id)
    if task.result is None or not task.result.commit:
        return ""
    wf = await c.store.get_workflow(task.workflow_id)
    if wf.execution is None or wf.repository_path is None:
        raise NotFoundError("workflow has no execution context")
    repo = c.worktrees.repository(wf.repository_path)
    commit = task.result.commit
    if task.kind == "integrate":
        return await c.git.diff(repo, wf.execution.base_commit, commit)
    parent = await c.git.rev_parse(repo, f"{commit}^1")
    return await c.git.diff(repo, parent, commit)


@router.get("/workflows/{workflow_id}/diff", response_class=PlainTextResponse)
async def workflow_diff(workflow_id: str, c: ContainerDep) -> str:
    """Everything the workflow changed: base commit -> integration commit."""
    wf = await c.store.get_workflow(workflow_id)
    if wf.execution is None or not wf.execution.integration_commit or wf.repository_path is None:
        return ""
    repo = c.worktrees.repository(wf.repository_path)
    return await c.git.diff(repo, wf.execution.base_commit, wf.execution.integration_commit)


@router.get("/workspaces")
async def list_workspaces(c: ContainerDep, workflow_id: str | None = None) -> list[Workspace]:
    return await c.store.list_workspaces(workflow_id)


@router.get("/workspaces/{workspace_id}")
async def get_workspace(workspace_id: str, c: ContainerDep) -> Workspace:
    return await c.store.get_workspace(workspace_id)

# workspaces/

ForgeFlow creates one git worktree per code-writing task here:

    workspaces/<workflow_id>/<task_key>      branch forgeflow/<workflow_id>/<task_key>
    workspaces/<workflow_id>/integration     branch forgeflow/<workflow_id>/integration

The branches live in the source repository under `repos/`, so you can inspect them
there with `git log forgeflow/<workflow_id>/integration` or `git diff main...<branch>`.

Worktrees created inside Docker are registered with container paths (`/workspaces/...`);
from Windows, use the branches in the source repository rather than these folders.

Contents of this folder are git-ignored (except this README).

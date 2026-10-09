# ForgeFlow terminal cockpit for Windows Terminal (additional.md section 8): four panes.
#   powershell -ExecutionPolicy Bypass -File ops\cockpit\cockpit.ps1
# Panes: Commander (runs/plans) | Monitor (health/guardrails) | Queue/workers | Triage/stop.
# Emergency stop from any pane:  uv run forgeflow run stop <trace_id> --reason "..."
# Routine autonomous execution does not depend on this window being open.
$ff = if ($env:FORGEFLOW_CLI) { $env:FORGEFLOW_CLI } else { "uv run forgeflow" }
$dir = (Resolve-Path "$PSScriptRoot\..\..").Path
wt -d "$dir" --title Commander powershell -NoExit -Command "$ff runs --watch" `; `
   split-pane -V -d "$dir" --title Monitor powershell -NoExit -Command "$ff monitor --watch" `; `
   move-focus left `; `
   split-pane -H -d "$dir" --title "Queue/workers" powershell -NoExit -Command "$ff runs --status RECEIVED,PLANNING,PLANNED,EXECUTING,VERIFYING,RETRYING --watch" `; `
   move-focus right `; `
   split-pane -H -d "$dir" --title "Triage/stop" powershell -NoExit -Command "$ff triage --watch"

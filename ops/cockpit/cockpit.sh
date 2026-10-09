#!/usr/bin/env bash
# ForgeFlow terminal cockpit (additional.md section 8) - tmux, four panes.
#   ./ops/cockpit/cockpit.sh            # FORGEFLOW_API_URL defaults to http://localhost:8000
#
#   +--------------------------+--------------------------+
#   | Commander                | Monitor                  |
#   | runs, plans, traces      | health, guardrails       |
#   +--------------------------+--------------------------+
#   | Queue / workers          | Triage / stop            |
#   | queued, active, finished | escalations, approvals,  |
#   |                          | alerts, emergency stop   |
#   +--------------------------+--------------------------+
#
# The cockpit is for operators. Autonomous runs never depend on it: triggers, the
# commander and workers run in the managed containers whether or not it is open.
# The Commander pane is also where an orchestration CLI (e.g. Codex CLI) can be
# started as the engineering anchor; Claude Code or another CLI can join as a partner.
set -euo pipefail
FF="${FORGEFLOW_CLI:-uv run forgeflow}"
SESSION="${FORGEFLOW_COCKPIT_SESSION:-forgeflow}"
command -v tmux >/dev/null || { echo "tmux is required (or use cockpit.ps1 on Windows)"; exit 1; }
tmux new-session -d -s "$SESSION" -n cockpit "$FF runs --watch"
tmux select-pane -t "$SESSION":0.0 -T Commander
tmux split-window -h -t "$SESSION":0 "$FF monitor --watch"
tmux select-pane -t "$SESSION":0.1 -T Monitor
tmux split-window -v -t "$SESSION":0.0 "$FF runs --status RECEIVED,PLANNING,PLANNED,EXECUTING,VERIFYING,RETRYING --watch"
tmux select-pane -t "$SESSION":0.2 -T "Queue/workers"
tmux split-window -v -t "$SESSION":0.1 "$FF triage --watch"
tmux select-pane -t "$SESSION":0.3 -T "Triage/stop"
tmux set-option -t "$SESSION" pane-border-status top
tmux attach -t "$SESSION"

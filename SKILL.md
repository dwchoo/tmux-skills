---
name: tmux-control
description: Use when Codex needs to operate tmux sessions, windows, or panes; run long-lived commands in tmux; send commands to a specific pane; capture pane output; or inspect tmux results before making follow-up code changes.
---

# tmux Control

## Purpose
Use tmux as Codex's long-running command workspace. Prefer stable pane IDs over pane positions, keep user-owned tmux state intact, and use visible manager panes plus status files for long-running jobs.

## Quick Start
1. Run `python scripts/tmux_control.py current` to identify the current tmux socket, session, window, and pane.
2. Run `python scripts/tmux_control.py list` when multiple sessions, windows, panes, or tmux sockets may exist.
3. If Codex is already inside tmux, use that current session/window by default.
4. If Codex is outside tmux, create or reuse a normal default-socket tmux session that is visible to `tmux ls`; report `tmux attach -t SESSION`.
5. Resolve human pane references before acting, for example `python scripts/tmux_control.py resolve --current-window --pane-index 3`.
6. Send commands with `python scripts/tmux_control.py send --pane <pane_id> --command '<command>' --enter --require-idle-shell` unless the pane is intentionally receiving input while busy.
7. Capture results with `python scripts/tmux_control.py capture --pane <pane_id> --lines 200`; add `--strip-ansi` for tqdm/color/control-heavy output.
8. For long-running commands, prefer `python scripts/tmux_control.py run --pane <pane_id> --command '<command>'` when a status/log record is enough.
9. For the default visible long-task workflow, start one idle Codex-owned manager first, then submit jobs with `manager submit`.
10. Use `manager run-next` only for sequential follow-up in the default worker pane after the relevant manager event has been acknowledged.
11. After receiving a manager event, inspect manager state once, run `manager ack --event-id EVENT`, then answer or queue one follow-up.
12. After `manager run-next`, stop the current repeat loop and wait for the next manager event; do not directly poll or monitor the worker pane.
13. For delayed checks or follow-up submissions, use managed `watch`, `queue-after-idle`, or `queue-after-status` instead of raw shell `sleep` watchers.
14. Add resume-only follow-up instructions with `run --next-instruction TEXT`, `run --next-instruction-file PATH`, or anchored `task add --after-job JOB_ID|--after-event EVENT_ID`.
15. Use `autopilot start` only when a Codex Desktop heartbeat will wake this thread to continue bounded repair work later.
16. Use `bridge register|start|status|cancel` only after the PoC app-server same-thread wake gate has passed for the workspace/thread.
17. Summarize important output and continue only when the captured result supports the next action.

## Target Selection
- Prefer the current tmux session/window when `$TMUX` is set. Long-running scripts, managed watches, queues, Autopilot attempts, and bridge daemons should run there by default.
- If Codex is outside tmux, use tmux's normal default socket and a session visible to `tmux ls` unless the user explicitly chooses another socket through `TMUX_TMPDIR`.
- Use `current`, `list`, and `resolve` before choosing targets when the target could be ambiguous.
- Treat `--pane-index` as tmux's usually 0-based `pane_index`; use `--ordinal` for human 1-based pane numbering.
- Always report the selected stable `pane_id`; when creating or reusing a session from outside tmux, also report the returned `attach_command`.

## Command Execution
- Use a current-session existing pane for new long-running work whenever possible; use `spawn` or `new-window` only when a new visible pane/window is needed.
- Use `send --no-enter` only when the user wants the command staged for review, and `send --enter` when Codex should run it.
- Prefer `send --require-idle-shell` for user-selected panes. If `send` warns that a script is not executable, use `bash path/to/script.sh`, `--bash-if-not-executable`, or fix the executable bit deliberately.
- Capture output before diagnosing failures, changing code, or claiming completion.
- Use `capture --strip-ansi` for progress bars or colored output, `--max-chars N` for large output, and positive integers for line-count flags.
- Avoid raw `tmux send-keys` unless the helper script is unavailable.

## Reading Output
- Read small output directly with `capture`.
- For large or monitored output, inspect the status tail first; escalate to full `log_path` or explicit `capture` only for `error`, `unclear`, or `needs_analysis`.
- Require subagent summaries to include `Can judge`, `Key conclusion`, `Important verbatim excerpts`, `Errors or risks`, `Recommended next action`, and `Uncertainty`.

## Long-Running Jobs and Managers
- `run`, `watch`, `queue-after-idle`, and `queue-after-status` store command files, logs, status JSON, acknowledgements, and managed worker records under `.codex/tmux-skills` by default.
- Managed worker contracts are canonical in [docs/managed-workers.md](docs/managed-workers.md). Real-use E2E coverage is canonical in [docs/real-use-e2e.md](docs/real-use-e2e.md).
- Detailed visible manager, bridge, tmux-inject, receipt recovery, sidecar, placeholder, and timing contracts are canonical in [docs/workflows-and-features.md](docs/workflows-and-features.md).
- Default visible long-task workflow: start one Codex-owned manager with `manager start --process-mode background`, keep worker output in visible panes, and use a compact reusable manager dashboard below Codex.
- Start the manager before long work. For bridge notification, run `manager bridge-check` before queueing work; for tmux-inject, start with `--notify tmux-inject --codex-pane PANE_ID|current` and bind exactly one verified Codex pane.
- `manager start --notify bridge` requires `--thread-id` and `--endpoint unix://PATH`. `manager start --notify tmux-inject` requires `--codex-pane PANE_ID|current`. Use `--notify none` only for manual visible-dashboard debugging.
- Manager events are the normal wake path. Codex should not rely on polling `manager status` except for manual diagnostics, tests, or details hidden from the compact pane.
- App-server or tmux prompt submission is not Codex receipt. Receipt is recorded only after main Codex runs `manager ack --event-id EVENT`.
- In manager-controlled tmux-inject mode, the wake prompt starts with `ID:<wake_id>;`, where `wake_id` is six lowercase hex characters. After the prompt, inspect manager state once, handle only the latest unacknowledged event, ack/report stale or handled events only, and wait for the next manager event after `manager run-next`.
- `manager run-next` starts sequential work in the default worker pane only, never creates a new worker pane, marks the previous terminal event as handled by the next job, and is blocked by active jobs or unacknowledged terminal events in bridge/tmux-inject modes.
- Completed jobs are preserved by default. `manager cleanup --jobs` removes manager-owned records and evidence only; it never closes panes or windows and refuses a live manager unless `--force` is passed.
- A fresh startup should inspect prior work explicitly with `task load --for-skill`; status files, follow-up tasks, and Autopilot objectives do not wake dormant Codex by themselves.
- Use [references/WORKFLOWS.md](references/WORKFLOWS.md) for copyable examples only.

## Safety Rules
- Run ordinary long-running commands when the user asks for tmux execution.
- Ask first for commands involving `sudo`, destructive file operations, process killing, secrets or credentials, deployments, production data, payments, or external state mutation.
- Do not kill panes, clear pane history, interrupt running processes, rename user tmux objects, detach clients, or close sessions unless explicitly requested.
- If a command may affect user data or external systems, state the risk and wait for confirmation.

## Helper Script
Use `scripts/tmux_control.py` from the skill directory. The helper uses only the Python standard library, but it imports sibling modules and launches sibling worker scripts, so copy or sync the whole `scripts/` directory for repository-local skill installations.

This quick reference lists common paths; run `python scripts/tmux_control.py COMMAND --help` for the complete flag set.

```bash
python scripts/tmux_control.py list
python scripts/tmux_control.py current [--target TARGET]
python scripts/tmux_control.py resolve [--target TARGET] [--current-window] [--pane-index 0..|--ordinal 1..]
python scripts/tmux_control.py spawn [--target SESSION:WINDOW] [--cwd PATH] [--vertical|--horizontal] [--percent 1..99]
python scripts/tmux_control.py new-window --cwd PATH [--target SESSION] [--name NAME]
python scripts/tmux_control.py send --pane PANE_ID --command TEXT [--require-idle-shell] [--strict-preflight] [--bash-if-not-executable] (--enter|--no-enter)
python scripts/tmux_control.py run --pane PANE_ID (--command TEXT|--command-file PATH) [--job-id ID] [(--next-instruction TEXT|--next-instruction-file PATH)] [--next-on succeeded|failed|terminal]
python scripts/tmux_control.py manager start [--manager-id ID] [--job-id ID (--command TEXT|--command-file PATH)] [--notify bridge --thread-id THREAD --endpoint unix://PATH|--notify tmux-inject --codex-pane (PANE_ID|current)|--notify none] [--dashboard-renderer pane|none] [--log-max-bytes N] [--process-mode foreground|background]
python scripts/tmux_control.py manager ps-poc
python scripts/tmux_control.py manager status [--manager-id ID]
python scripts/tmux_control.py manager bridge-check [--manager-id ID] [--ack-timeout-seconds N]
python scripts/tmux_control.py manager ack [--manager-id ID] --event-id EVENT [--turn-id TURN] [--note TEXT]
python scripts/tmux_control.py manager submit [--manager-id ID] [--pane PANE_ID|--new-worker] --job-id ID (--command TEXT|--command-file PATH)
python scripts/tmux_control.py manager run-next [--manager-id ID] --job-id ID (--command TEXT|--command-file PATH)
python scripts/tmux_control.py manager cancel [--manager-id ID] [--stop-worker]
python scripts/tmux_control.py manager cleanup [--manager-id ID] [--jobs] [--force]
python scripts/tmux_control.py watch --job-id ID --pane PANE_ID [--interval N] [--capture-lines N] [--status-lines N] [--status-max-chars N] [--status-file PATH] [--low-token] [--timeout-seconds N] [--replace] [--allow-duplicate]
python scripts/tmux_control.py watch list [--compact] [--no-observed-tail] [--max-chars N]
python scripts/tmux_control.py watch status|cancel --job-id ID [--compact] [--include-pane-state]
python scripts/tmux_control.py watch gc --stale [--dry-run] [--compact] [--include-pane-state]
python scripts/tmux_control.py queue-after-idle --job-id ID (--pane|--then-pane) PANE_ID ((--command|--then-command) TEXT|--command-file PATH) [--interval N|--poll-seconds N] [--timeout-seconds N] [--then-require-idle-shell] [--replace] [--allow-duplicate]
python scripts/tmux_control.py queue-after-status --job-id ID --status-file PATH --require-row KEY:VALUE|key=value,... (--pane|--then-pane) PANE_ID ((--command|--then-command) TEXT|--command-file PATH) [--interval N|--poll-seconds N] [--timeout-seconds N] [--low-token] [--then-require-idle-shell|--no-require-idle-shell] [--replace] [--allow-duplicate]
python scripts/tmux_control.py job list [--compact] [--no-observed-tail] [--max-chars N]
python scripts/tmux_control.py job status|cancel --job-id ID [--compact] [--include-pane-state]
python scripts/tmux_control.py job gc --stale [--dry-run] [--compact] [--include-pane-state]
python scripts/tmux_control.py autopilot start --objective-id ID --pane PANE_ID (--command TEXT|--command-file PATH) --goal TEXT [--cwd PATH] [--max-attempts N]
python scripts/tmux_control.py autopilot tick --objective-id ID [--for-agent] [--json] [--max-chars N]
python scripts/tmux_control.py autopilot evidence --objective-id ID --kind status|log [--attempt current|N] [--max-chars N]
python scripts/tmux_control.py autopilot rerun --objective-id ID [(--command TEXT|--command-file PATH)]
python scripts/tmux_control.py autopilot status|heartbeat-prompt|complete|cancel --objective-id ID
python scripts/tmux_control.py autopilot block --objective-id ID --reason TEXT
python scripts/e2e_real_use.py --scenario smoke
python scripts/e2e_real_use.py --scenario all --json
python scripts/tmux_control.py task load [--for-skill] [--json] [--max-items N]
python scripts/tmux_control.py task next [--json]
python scripts/tmux_control.py task claim --task-id TASK_ID
python scripts/tmux_control.py task add [--task-id TASK_ID] (--after-job JOB_ID|--after-event EVENT_ID) --trigger-on succeeded|failed|terminal --instruction TEXT
python scripts/tmux_control.py bridge register --thread-id THREAD --endpoint unix://PATH [--bridge-id ID] [--poll-seconds N] [--quiet-seconds N] [--replace]
python scripts/tmux_control.py bridge start --bridge-id ID [--foreground|--background] [--replace]
python scripts/tmux_control.py bridge status --bridge-id ID [--json]
python scripts/tmux_control.py bridge cancel --bridge-id ID
python scripts/tmux_control.py monitor --pane PANE_ID [--match-regex REGEX] [--idle-shell] [--timeout-seconds N] [--status-lines N] [--status-max-chars N]
python scripts/tmux_control.py capture --pane PANE_ID [--lines N] [--strip-ansi] [--max-chars N]
```

The script prints JSON so Codex can preserve stable IDs across later steps. `list` and `resolve` include diagnostic fields such as `pane_pid`, `pane_dead`, pane size, TTY, child process count, and a bounded descendant process summary.

---
name: tmux-control
description: Use when Codex needs to operate tmux sessions, windows, or panes; run long-lived commands in tmux; send commands to a specific pane; capture pane output; or inspect tmux results before making follow-up code changes.
---

# tmux Control

## Purpose
Use tmux as Codex's long-running command workspace. Prefer stable pane IDs over pane positions, keep user-owned tmux state intact, and use status files plus Codex command hooks for long-running jobs.

## Quick Start
1. Run `python scripts/tmux_control.py current` to identify the tmux socket, session, window, and pane that commands will use.
2. Run `python scripts/tmux_control.py list` when multiple sessions, windows, panes, or tmux sockets may exist.
3. If Codex is already running inside tmux, use that current tmux session/window by default.
4. If Codex is running outside tmux, create or reuse a normal default-socket tmux session so the user can see it with `tmux ls` and attach with `tmux attach -t SESSION`. Do not default to a hidden `TMUX_TMPDIR`.
5. Resolve human pane references before acting, for example `python scripts/tmux_control.py resolve --current-window --pane-index 3`.
6. Send commands with `python scripts/tmux_control.py send --pane <pane_id> --command '<command>' --enter --require-idle-shell` unless the pane is explicitly intended to receive input while busy.
7. Capture results with `python scripts/tmux_control.py capture --pane <pane_id> --lines 200`; add `--strip-ansi` for tqdm/color/control-heavy output.
8. For long-running commands, use `python scripts/tmux_control.py run --pane <pane_id> --command '<command>'` so completion/failure is recorded under `.codex/tmux-skills`.
9. For delayed checks or follow-up submissions, use managed `watch`, `queue-after-idle`, or `queue-after-status` instead of raw shell `sleep` watchers.
10. Add resume-only follow-up instructions with `run --next-instruction TEXT`, `run --next-instruction-file PATH`, or anchored `task add --after-job JOB_ID|--after-event EVENT_ID`.
11. Use `autopilot start` only when a Codex Desktop heartbeat will wake this thread to continue bounded repair work later.
12. Use `bridge register|start|status|cancel` only after the PoC app-server same-thread wake gate has passed for the workspace/thread.
13. Summarize the important output and continue with code edits or further tmux commands only when the result supports it.

## Target Selection
- Prefer the current tmux session/window when `$TMUX` is set. Every process that the user should observe, including long-running scripts, managed watches, queues, Autopilot attempts, and bridge daemons, should run in that session by default.
- If Codex is outside tmux, use tmux's normal default socket. A created session must be visible to `tmux ls` unless the user explicitly chooses another socket through `TMUX_TMPDIR`.
- Use `current` before choosing targets when more than one session, window, or pane exists.
- Use `current --target <target>` to inspect a specific pane/window/session from outside tmux.
- Use `resolve` to turn a pane index, ordinal, or explicit target into a stable `pane_id` before sending commands.
- Treat `--pane-index` as tmux's usually 0-based `pane_index`; use `--ordinal` for human 1-based pane numbering.
- Outside tmux, create or reuse a codex-managed session on the default tmux socket. Report `tmux attach -t SESSION` so the user can enter it.
- Use explicit user-provided targets when present.
- Always report the selected `pane_id` after creating or choosing a pane.
- When creating or reusing a session from outside tmux, also report the returned `attach_command`.
- If outside-tmux session creation fails because the sandbox cannot create a tmux socket, rerun the same helper command with the required filesystem/process escalation.

## Command Execution
- Use a current-session existing pane for new long-running work whenever possible.
- Use `spawn` or `new-window` in the current session when a new pane/window is needed for foreground visibility.
- Use `send --no-enter` only when the user wants the command staged for review.
- Use `send --enter` when the user has asked Codex to run the command.
- Prefer `send --require-idle-shell` for user-selected panes; it refuses to send when the target is not an idle shell prompt or has child processes.
- If `send` warns that a script is not executable, use `bash path/to/script.sh`, `--bash-if-not-executable`, or fix the executable bit deliberately.
- Capture output before diagnosing failures, changing code, or claiming completion.
- Use `capture --strip-ansi` for progress bars, colored output, or terminal-control-heavy logs.
- Use `capture --max-chars N` when output is large; truncation happens after optional ANSI stripping. `N` must be non-negative, and `0` intentionally omits captured output while reporting truncation metadata.
- Use positive integers for line-count flags such as `--lines` and `--capture-lines`.
- Avoid raw `tmux send-keys` unless the helper script is unavailable.

## Reading Output
- Read small or simple pane output directly with `capture`.
- For large or monitored output, first inspect only the status tail written by `watch` or `monitor`; by default this is the last 10 lines capped to 1200 characters.
- Use the latest available lightweight/mini model for this first pass, or the main model with `reasoning_effort=low` when model selection is not available.
- If the first pass indicates `error`, `unclear`, or `needs_analysis`, inspect the full `log_path` or an explicit `capture` with `reasoning_effort=medium`.
- If the first pass indicates `progressing` or `complete`, keep `reasoning_effort=low` and report only the concise conclusion and next action to the main conversation.
- Require this subagent format: `Can judge`, `Key conclusion`, `Important verbatim excerpts`, `Errors or risks`, `Recommended next action`, `Uncertainty`.
- If the subagent cannot judge confidently, capture the relevant pane output in the main agent and inspect it directly.

## Long-Running Jobs and Hooks
- `run` stores command files, logs, status JSON, and acknowledgements in `.codex/tmux-skills` by default.
- Prefer `python scripts/tmux_control.py run` for long-running commands. If the helper is unavailable and a manual raw tmux fallback is required, launch the installed wrapper from the skill directory: `tmux new-session -d -s <job_id> "cd ~/.codex/skills/tmux-control && bash scripts/run_managed_job.sh <workspace>/logs/jobs/<job_id> <command> <args...>"`.
- The fallback wrapper preserves `command.sh`, combined `stdout.log`, `status.json`, `exitcode`, `started_at`, `finished_at`, and direct child `pid`; it executes argv without shell re-parsing and does not create `stderr.log`.
- Managed worker contracts are canonical in [docs/managed-workers.md](docs/managed-workers.md).
- Real-use E2E coverage is canonical in [docs/real-use-e2e.md](docs/real-use-e2e.md).
- `watch`, `queue-after-idle`, and `queue-after-status` store managed worker records in `.codex/tmux-skills/jobs`; inspect them with compact output first, for example `watch list --compact --no-observed-tail` or `job status --compact`.
- Use `queue-after-idle` when the next command should run only after a busy pane returns to an idle shell.
- Use `queue-after-status` when a status TSV must reach required row states before the next command is submitted.
- Cancel managed background workers with `watch cancel --job-id ID` or `job cancel --job-id ID`.
- Use `watch --low-token --status-file PATH` for long monitoring where status-file polling is enough; it avoids normal pane captures.
- Use `job gc --stale` or `watch gc --stale` when `effective_status` shows dead, orphaned, or stale managed workers. GC marks records as stale and preserves evidence.
- Use `--include-pane-state` when status, cancel, or GC output should report whether the target pane still exists. Do not close panes or windows unless the user explicitly asks.
- `run --next-instruction`, `run --next-instruction-file`, and anchored `task add` store Codex instructions in `.codex/tmux-skills/tasks`; they do not execute while Codex is absent.
- Codex hooks do not directly observe independent tmux jobs. Use `SessionStart`, `UserPromptSubmit`, and `Stop` command hooks to read status files.
- `SessionStart` resume/compact context can expose ready follow-up tasks. A new startup should use explicit `task load --for-skill` instead of auto-running prior work.
- `Stop` can continue a current turn for an unacknowledged terminal event or ready task; hooks do not wake a dormant thread by themselves.
- `autopilot` objectives do not wake Codex by themselves. Pair them with a current-thread Codex Desktop heartbeat using `autopilot heartbeat-prompt`.
- `bridge` observes terminal events and ready tasks under `.codex/tmux-skills` and wakes a user-specified main Codex thread through the same local `codex app-server`.
- Bridge wake prompts are path-only. They include workspace/job/status/task/log paths, but never status/log summaries, task instruction bodies, traceback text, diagnosis, suggested commands, or retry instructions.
- Bridge v1 uses stdlib WebSocket-over-Unix transport for explicit `unix://PATH` endpoints only. It does not call the OpenAI API directly, require `OPENAI_API_KEY`, discover threads, run standalone `codex`, run `codex app-server proxy`, run `codex exec resume`, call `turn/steer`, call `thread/shellCommand`, or send keys to tmux panes.
- Autopilot uses adaptive context: start with `tick --for-agent --max-chars 1200`, then use bounded `autopilot evidence` only when the summary is insufficient.
- Autopilot bounded repair allows workspace diagnostics, code/config edits, focused tests, and rerun. Block before destructive cleanup, force git operations, push/deploy, dependency installation, secrets/auth changes, or expanding to higher-cost/longer training.
- Use `references/HOOKS.md` for copyable hook snippets and `references/WORKFLOWS.md` for examples.

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

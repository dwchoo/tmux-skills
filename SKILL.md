---
name: tmux-control
description: Use when Codex needs to operate tmux sessions, windows, or panes; run long-lived commands in tmux; send commands to a specific pane; capture pane output; or inspect tmux results before making follow-up code changes.
---

# tmux Control

## Purpose
Use tmux as Codex's long-running command workspace. Prefer stable pane IDs over pane positions, keep user-owned tmux state intact, and use status files plus Codex command hooks for long-running jobs.

## Quick Start
1. Run `python scripts/tmux_control.py current` to identify Codex's current tmux session, window, and pane.
2. Run `python scripts/tmux_control.py list` when multiple sessions, windows, or panes may exist.
3. If Codex is inside tmux, use the current session/window by default.
4. If Codex is outside tmux, use `python scripts/tmux_control.py spawn` or `new-window` to create or reuse a detached session named `codex-<workspace-basename>`.
5. Resolve human pane references before acting, for example `python scripts/tmux_control.py resolve --current-window --pane-index 3`.
6. Send commands with `python scripts/tmux_control.py send --pane <pane_id> --command '<command>' --enter --require-idle-shell` unless the pane is explicitly intended to receive input while busy.
7. Capture results with `python scripts/tmux_control.py capture --pane <pane_id> --lines 200`; add `--strip-ansi` for tqdm/color/control-heavy output.
8. For long-running commands, use `python scripts/tmux_control.py run --pane <pane_id> --command '<command>'` so completion/failure is recorded under `.codex/tmux-skills`.
9. Add resume-only follow-up instructions with `run --next-instruction TEXT` or `task add`.
10. Summarize the important output and continue with code edits or further tmux commands only when the result supports it.

## Target Selection
- Prefer the current tmux window only when `$TMUX` is set.
- Use `current` before choosing targets when more than one session, window, or pane exists.
- Use `current --target <target>` to inspect a specific pane/window/session from outside tmux.
- Use `resolve` to turn a pane index, ordinal, or explicit target into a stable `pane_id` before sending commands.
- Treat `--pane-index` as tmux's usually 0-based `pane_index`; use `--ordinal` for human 1-based pane numbering.
- Outside tmux, create or reuse a codex-managed detached session for the current workspace.
- Use explicit user-provided targets when present.
- Always report the selected `pane_id` after creating or choosing a pane.
- When creating or reusing a detached session, also report the returned `attach_command`.
- If outside-tmux session creation fails because the sandbox cannot create a tmux socket, rerun the same helper command with the required filesystem/process escalation.

## Command Execution
- Use `spawn` for new long-running work unless the user explicitly chooses an existing pane.
- Use `send --no-enter` only when the user wants the command staged for review.
- Use `send --enter` when the user has asked Codex to run the command.
- Prefer `send --require-idle-shell` for user-selected panes; it refuses to send when the target is not an idle shell prompt or has child processes.
- Capture output before diagnosing failures, changing code, or claiming completion.
- Use `capture --strip-ansi` for progress bars, colored output, or terminal-control-heavy logs.
- Use `capture --max-chars N` when output is large; truncation happens after optional ANSI stripping.
- Avoid raw `tmux send-keys` unless the helper script is unavailable.

## Reading Output
- Read small or simple pane output directly with `capture`.
- For large output, ask a subagent to inspect the captured text with inherited model settings and `reasoning_effort=low`; use `medium` only when the content is likely ambiguous.
- Do not use fast/spark models for log interpretation.
- Require this subagent format: `Can judge`, `Key conclusion`, `Important verbatim excerpts`, `Errors or risks`, `Recommended next action`, `Uncertainty`.
- If the subagent cannot judge confidently, capture the relevant pane output in the main agent and inspect it directly.

## Long-Running Jobs and Hooks
- `run` stores command files, logs, status JSON, and acknowledgements in `.codex/tmux-skills` by default.
- `run --next-instruction` and `task add` store Codex instructions in `.codex/tmux-skills/tasks`; they do not execute while Codex is absent.
- Codex hooks do not directly observe independent tmux jobs. Use `SessionStart`, `UserPromptSubmit`, and `Stop` command hooks to read status files.
- `SessionStart` resume/compact context can expose ready follow-up tasks. A new startup should use explicit `task load --for-skill` instead of auto-running prior work.
- `Stop` can continue a current turn for an unacknowledged terminal event or ready task; hooks do not wake a dormant thread by themselves.
- Use `references/HOOKS.md` for copyable hook snippets and `references/WORKFLOWS.md` for examples.

## Safety Rules
- Run ordinary long-running commands when the user asks for tmux execution.
- Ask first for commands involving `sudo`, destructive file operations, process killing, secrets or credentials, deployments, production data, payments, or external state mutation.
- Do not kill panes, clear pane history, interrupt running processes, rename user tmux objects, detach clients, or close sessions unless explicitly requested.
- If a command may affect user data or external systems, state the risk and wait for confirmation.

## Helper Script
Use `scripts/tmux_control.py` from the skill directory. The helper is self-contained so it can also be copied into a repository-local skill installation.

```bash
python scripts/tmux_control.py list
python scripts/tmux_control.py current [--target TARGET]
python scripts/tmux_control.py resolve [--target TARGET] [--current-window] [--pane-index N|--ordinal N]
python scripts/tmux_control.py spawn [--target SESSION:WINDOW] [--cwd PATH] [--vertical|--horizontal] [--percent N]
python scripts/tmux_control.py new-window --cwd PATH [--target SESSION] [--name NAME]
python scripts/tmux_control.py send --pane PANE_ID --command TEXT [--require-idle-shell] [--enter|--no-enter]
python scripts/tmux_control.py run --pane PANE_ID (--command TEXT|--command-file PATH) [--job-id ID] [--next-instruction TEXT]
python scripts/tmux_control.py task load [--for-skill] [--json]
python scripts/tmux_control.py task next [--json]
python scripts/tmux_control.py task claim --task-id TASK_ID
python scripts/tmux_control.py monitor --pane PANE_ID [--match-regex REGEX] [--idle-shell]
python scripts/tmux_control.py capture --pane PANE_ID [--lines N] [--strip-ansi] [--max-chars N]
```

The script prints JSON so Codex can preserve stable IDs across later steps. `list` and `resolve` include diagnostic fields such as `pane_pid`, `pane_dead`, pane size, TTY, child process count, and a bounded descendant process summary.

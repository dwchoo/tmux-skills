# Codex Hooks for tmux-skills

These snippets use current Codex command hook syntax. They do not install or edit Codex config automatically.

## SessionStart

```toml
[[hooks.SessionStart]]
matcher = "resume|compact"

[[hooks.SessionStart.hooks]]
type = "command"
command = "python scripts/codex_tmux_hook.py context --event SessionStart --workspace \"$PWD\""
timeout = 5
```

## UserPromptSubmit

```toml
[[hooks.UserPromptSubmit]]
matcher = "*"

[[hooks.UserPromptSubmit.hooks]]
type = "command"
command = "python scripts/codex_tmux_hook.py context --event UserPromptSubmit --workspace \"$PWD\""
timeout = 5
```

## Stop

```toml
[[hooks.Stop]]
matcher = "*"

[[hooks.Stop.hooks]]
type = "command"
command = "python scripts/codex_tmux_hook.py stop --workspace \"$PWD\""
timeout = 5
```

## Notes

- `PostToolUse` observes Codex tool calls, not independent long-running tmux jobs.
- Hooks read status files written by `tmux_job.py` and `tmux_monitor.py`, plus follow-up tasks under `.codex/tmux-skills/tasks`.
- A fresh startup should not auto-run prior work. Use `python scripts/tmux_control.py task load --for-skill` to inspect old work explicitly.
- Hooks do not wake a dormant Codex thread by themselves. `Stop` can continue an active turn; `SessionStart` resume/compact injects ready task context when a prior Codex CLI session is resumed.
- `codex_tmux_hook.py stop` treats empty, malformed, or non-object stdin as empty input, and returns no block when Codex sends `stop_hook_active: true`.
- Hook output compacts multiline status fields, managed job fields, and ready task instructions, and bounds long text so a large task file cannot dominate Codex hook context.
- Add `--state-dir PATH` to the hook commands when the workspace uses a custom tmux-skills state directory.

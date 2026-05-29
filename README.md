# tmux-skills

Codex skill for controlled tmux usage. It provides concise skill instructions, Python helpers for tmux panes/windows, long-running job wrappers, resumable follow-up task records, hook status readers, and reusable pane monitors.

## Requirements

- Python 3.10+
- `tmux` available on `PATH`

## Files

- `SKILL.md`: skill instructions and safety rules.
- `references/`: copyable hook snippets and workflow examples.
- `scripts/tmux_control.py`: main tmux helper used by the skill.
- `scripts/tmux_job.py`: long-running command wrapper.
- `scripts/tmux_monitor.py`: single-trigger pane monitor.
- `scripts/codex_tmux_hook.py`: command hook status reader.
- `.codex/tmux-skills/tasks/`: runtime follow-up task records created by `task` and `run --next-instruction`.

## Verify

```bash
python3 -m py_compile scripts/tmux_control.py
python3 -m py_compile scripts/tmux_job.py scripts/tmux_monitor.py scripts/codex_tmux_hook.py
python3 -m unittest discover
python3 scripts/tmux_control.py --help
python3 scripts/tmux_control.py list
```

When outside tmux, `spawn` and `new-window` create or reuse a detached `codex-<workspace>` session and report an `attach_command`.

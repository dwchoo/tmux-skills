# tmux-skills Workflows

## Long-running command

```bash
python scripts/tmux_control.py spawn --cwd "$PWD"
python scripts/tmux_control.py run --pane %1 --command "python train.py" --name training --next-instruction "Inspect the training result and choose the next experiment."
python scripts/tmux_control.py capture --pane %1 --lines 200 --strip-ansi --max-chars 12000
```

`run` writes a command file under `.codex/tmux-skills/commands`, mirrors command output to the pane and log file, records terminal status as JSON, and can create a waiting follow-up task. The follow-up task becomes ready after the configured terminal result; it does not run while Codex is absent.

## Resume or load prior work

```bash
python scripts/tmux_control.py task load --for-skill
python scripts/tmux_control.py task next --json
python scripts/tmux_control.py task claim --task-id TASK_ID
```

Use `task load --for-skill` in a new Codex session to quickly understand prior tmux work. In a resumed Codex CLI session, the `SessionStart` hook can inject the same ready-task clue automatically.

## Background monitor

```bash
python scripts/tmux_control.py monitor --pane %1 --match-regex "ERROR|Traceback" --lines 200
```

The monitor is single-trigger. It exits after a match, timeout, idle-shell event, or stop signal, then writes status JSON.

## Large output review

Use main-agent capture for short output. For large output, capture with `--max-chars`, delegate the text to a subagent, and require this structure:

```text
Can judge:
Key conclusion:
Important verbatim excerpts:
Errors or risks:
Recommended next action:
Uncertainty:
```

If the subagent says it cannot judge, inspect the relevant output directly in the main agent.

## Hook status flow

1. `tmux_job.py` or `tmux_monitor.py` writes terminal status.
2. `codex_tmux_hook.py context` reports useful status and ready task instructions on `SessionStart` resume/compact or `UserPromptSubmit`.
3. `codex_tmux_hook.py stop` returns `decision: block` for the newest ready task or unacknowledged terminal event. It does not claim or complete tasks.

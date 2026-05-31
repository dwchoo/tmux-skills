# tmux-skills Workflows

## Long-running command

```bash
python scripts/tmux_control.py spawn --cwd "$PWD"
python scripts/tmux_control.py run --pane %1 --command "python train.py" --name training --next-instruction "Inspect the training result and choose the next experiment."
python scripts/tmux_control.py capture --pane %1 --lines 200 --strip-ansi --max-chars 12000
```

`run` writes a command file under `.codex/tmux-skills/commands`, mirrors command output to the pane and log file, records terminal status as JSON, and can create a waiting follow-up task. The follow-up task becomes ready after the configured terminal result; it does not run while Codex is absent.
Blank command text, whitespace-only command files, blank `--command-file` paths, blank `--next-instruction-file` paths, and blank explicit `--job-id` values are rejected before anything is sent to tmux. The internal job wrapper also records a failed status instead of treating a blank command file as a successful shell no-op.

For `queue-after-status`, the status file path and row specs must be nonblank; blank row specs are never treated as wildcard matches.

## Resume or load prior work

```bash
python scripts/tmux_control.py task load --for-skill
python scripts/tmux_control.py task next --json
python scripts/tmux_control.py task claim --task-id TASK_ID
```

Use `task load --for-skill` in a new Codex session to quickly understand prior tmux work. In a resumed Codex CLI session, the `SessionStart` hook can inject the same ready-task clue automatically.

Use `--max-items N` with a positive integer when you need a shorter load report.
Text task reports compact multiline fields and bound long task instructions or output tails; use `--json` or the evidence files when the full stored text is needed.
`task list` shows unresolved waiting, ready, in-progress, and blocked tasks by default; use `--all` to include done or cancelled tasks.

Manual follow-up tasks must be anchored to a specific terminal event or job:

```bash
python scripts/tmux_control.py task add --after-job training --trigger-on succeeded --instruction "Inspect the completed training run."
```

Blank `--after-job`, `--after-event`, instruction, or explicit `--task-id` values are rejected so stored tasks stay anchored and actionable.
Commands that mutate a task, such as `task claim`, `task done`, `task blocked`, and `task cancel`, require a nonblank `--task-id`.

## Background monitor

```bash
python scripts/tmux_control.py monitor --pane %1 --match-regex "ERROR|Traceback" --lines 200
```

The monitor is single-trigger. It exits after a match, timeout, idle-shell event, or stop signal, then writes status JSON.
Monitor pane targets must be nonblank; internal wrapper ids are also rejected before status files are written.

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
3. `codex_tmux_hook.py stop` returns `decision: block` for the next ready task or newest unacknowledged terminal event. It does not claim or complete tasks. When a ready task is tied to a terminal event, Stop acknowledges that event so the same event is not reported again after the task is handled. If acknowledgement fails, Stop still returns the block reason and includes an acknowledgement warning. If state files are unreadable, Stop includes a skipped-file warning with the block reason.

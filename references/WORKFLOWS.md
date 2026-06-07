# tmux-skills Workflows

Copyable examples only; the full manager, bridge, tmux-inject, receipt, sidecar, and timing contract lives in [`docs/workflows-and-features.md`](../docs/workflows-and-features.md).

## Long-running command

```bash
python scripts/tmux_control.py spawn --cwd "$PWD"
python scripts/tmux_control.py run --pane %1 --command "python train.py" --name training --next-instruction "Inspect the training result and choose the next experiment."
python scripts/tmux_control.py capture --pane %1 --lines 200 --strip-ansi --max-chars 12000
```

`run` records command, log, status, and optional follow-up task evidence under `.codex/tmux-skills`. Follow-up tasks become ready after the configured terminal result; they do not run while Codex is absent.

## Visible manager start and submit

Bridge backend for a verified `codex --remote` app-server thread:

```bash
python scripts/tmux_control.py manager start --notify bridge --thread-id THREAD --endpoint unix://PATH
python scripts/tmux_control.py manager bridge-check
python scripts/tmux_control.py manager submit --job-id train-1 --command "python train.py"
```

tmux-inject backend for an already-open Codex TUI in one verified pane:

```bash
python scripts/tmux_control.py manager start --notify tmux-inject --codex-pane %1
python scripts/tmux_control.py manager submit --job-id train-1 --command "python train.py"
```

Use the default visible manager workflow for long tasks that should keep worker output readable and notify Codex on terminal events. Start the manager before starting long work. Use `manager submit --new-worker` for parallel visible work.

## Manager event handling

When Codex receives a manager event, inspect manager state once, acknowledge the event, then answer or queue one follow-up:

```bash
python scripts/tmux_control.py manager status
python scripts/tmux_control.py manager ack --event-id EVENT_ID
python scripts/tmux_control.py manager submit --job-id train-2 --command "python eval.py"
python scripts/tmux_control.py manager run-next --job-id report-1 --command "python report.py"
```

In manager-controlled tmux-inject mode, the wake prompt starts with:

```text
ID:<hex6>;
```

Handle only the latest unacknowledged event. If the event is stale or already handled, ack/report only. After `manager run-next`, wait for the next manager event; do not poll or monitor the worker pane directly.

## Follow-up and cleanup

```bash
python scripts/tmux_control.py manager cancel
python scripts/tmux_control.py manager cancel --stop-worker
python scripts/tmux_control.py manager cancel --job-id train-2
python scripts/tmux_control.py manager cancel --all-workers
python scripts/tmux_control.py manager cleanup --jobs
```

`manager cancel` leaves panes/windows and evidence intact by default. `manager cleanup --jobs` removes manager-owned records and evidence for throwaway work; it never closes panes or windows.

## Resume or load prior work

```bash
python scripts/tmux_control.py task load --for-skill
python scripts/tmux_control.py task next --json
python scripts/tmux_control.py task claim --task-id TASK_ID
```

Use `task load --for-skill` in a new Codex session to inspect prior tmux work. Use `task next --json` when you need the next ready task in machine-readable form.

Manual follow-up tasks must be anchored to a terminal event or job:

```bash
python scripts/tmux_control.py task add --after-job training --trigger-on succeeded --instruction "Inspect the completed training run."
```

## Background monitor

```bash
python scripts/tmux_control.py monitor --pane %1 --match-regex "ERROR|Traceback" --lines 200
```

The monitor exits after a match, timeout, idle-shell event, or stop signal, then writes status JSON with a compact `last_output` tail by default.

## Large output review

Use main-agent `capture` for short output. For large or monitored output, inspect the capped status tail first. Escalate to full `log_path` or explicit `capture` only when the first pass reports `error`, `unclear`, or `needs_analysis`.

```text
Can judge:
Key conclusion:
Important verbatim excerpts:
Errors or risks:
Recommended next action:
Uncertainty:
```

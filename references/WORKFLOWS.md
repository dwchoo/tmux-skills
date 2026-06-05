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

## Visible manager long task

```bash
python scripts/tmux_control.py manager start --notify bridge --thread-id THREAD --endpoint unix://"$PWD/.codex/tmux-skills/bridge/sockets/codex-bridge.sock"
python scripts/tmux_control.py manager bridge-check
python scripts/tmux_control.py manager run-next --job-id train-1 --command "python train.py"
```

Use this as the default long-task workflow when the manager process should remain visible to Codex `/ps`, worker output should stay readable in a long side pane, and one reusable compact manager pane should remain visible below Codex in tmux. Start one manager first, then submit the first and later scripts with `manager run-next`. Inside tmux it uses the current session/window. Outside tmux it creates a normal visible default-socket session and prints the `tmux attach -t SESSION` command.

`manager start` is foreground and Codex-owned by default. Keep the command running so Codex `/ps` can report it. If Codex exits, this manager loop exits too; worker jobs already submitted to the tmux worker pane continue independently.

The manager writes `.codex/tmux-skills/managers/<manager_id>.json`, starts the worker through `tmux_control.py run`, and updates the reusable manager pane with heartbeat, status path, log path, task path, and worker pane details. The manager pane must not run a persistent renderer loop. Manager-owned logs are trimmed to a bounded tail while the manager is alive. When Codex receives a bridge turn, it should read the manager path from the prompt, acknowledge the current target event, then use `capture` on the worker pane for live terminal output when responding. For bridge preflight prompts, acknowledge `bridge_verification.event_id`; for terminal prompts, acknowledge `last_terminal_event_id`. On success, failure, stop, timeout, cancellation, stale status, or missing worker pane, the Codex-owned process stays alive as `waiting_for_codex`.

When no worker pane is assigned, `manager start` splits Codex vertically first so the worker gets a tall right-side pane. Only the compact manager pane is placed below Codex. Repeated starts reuse the idle manager pane directly below Codex and reuse an existing idle tall worker pane when possible. A single manager record owns multiple job ids instead of creating a new manager for each job.
By default the manager id is stable per workspace/session/window. Pass `--manager-id` only when you need an explicit compatibility id.
If that manager process is already alive, another `manager start` exits instead of creating a second manager loop. Submit work with `manager run-next`; the same manager monitors the first and subsequent jobs.

Bridge notifications are path-only and are submitted by the manager, not by Codex polling status files. They include workspace, manager path, job/status/log/task paths, and no status summaries, log excerpts, task instruction bodies, diagnosis, retry commands, or model/delegation text. `--notify bridge` requires `--thread-id` and `--endpoint unix://PATH`; use `--notify none` only for manual visible-dashboard debugging. Bridge mode is supported only when target Codex is attached to that app-server endpoint with `codex --remote unix://PATH`; an ordinary standalone `codex` or `codex --yolo` pane is not a verified target, and `CODEX_THREAD_ID` alone is not enough.
Successful app-server submission is recorded once per terminal event in `submitted_event_ids` and `last_notification.submitted_to_app_server`; it does not prove Codex received or acted on the turn. Codex receipt is recorded only after main Codex runs `manager ack --event-id <last_terminal_event_id>`.
Run `manager bridge-check` before queueing work. It creates a path-only preflight event, submits it through the configured endpoint, and waits for target Codex to run `manager ack`. Verified bridge receipt is bound to `manager_id`, `workspace`, `endpoint`, `thread_id`, and the preflight event/prompt hash; changing any value invalidates it. In bridge mode, `manager run-next` and `manager start` with an initial command refuse to queue worker commands until bridge receipt is verified. After a terminal event, `run-next` is blocked until that event is acknowledged, then the previous event is marked `handled`.
If bridge submission fails, the live manager retries while it remains in `waiting_for_codex` and does not mark the event submitted until submission succeeds. Use `manager status` only for manual diagnostics or tests: `heartbeat_at` advances while alive, `last_terminal_event_id` records the observed terminal event, and `notifications` shows lifecycle states such as `awaiting_ack`, `acknowledged`, and `handled`. A demo is not successful until target Codex receives the bridge turn and records `manager ack`; polling status manually only proves diagnostics.

Follow-up and cancellation:

```bash
python scripts/tmux_control.py manager status
python scripts/tmux_control.py manager bridge-check
python scripts/tmux_control.py manager ack --event-id EVENT_ID
python scripts/tmux_control.py manager run-next --job-id train-2 --command "python eval.py"
python scripts/tmux_control.py manager cancel
python scripts/tmux_control.py manager cancel --stop-worker
python scripts/tmux_control.py manager cleanup --jobs
```

`manager cancel` leaves panes/windows and evidence intact by default. `--stop-worker` is required before cancellation attempts to stop the active worker job. Cancellation is sticky: after `cancel_requested` or `cancelled` is recorded, delayed bridge acknowledgements, terminal notifications, or bridge-check updates must not revive the manager as `waiting_for_codex`, `idle`, or `running`. Use `manager cleanup --jobs` after demos or throwaway work to remove the cancelled manager record, dashboard, and manager-owned command/status/log files. Cleanup never closes panes or windows, and refuses a live manager unless `--force` is passed.

## Resume or load prior work

```bash
python scripts/tmux_control.py task load --for-skill
python scripts/tmux_control.py task next --json
python scripts/tmux_control.py task claim --task-id TASK_ID
```

Use `task load --for-skill` in a new Codex session to quickly understand prior tmux work. This is the explicit resume path for ready tasks, recent jobs, running managed workers, and stale records.
Use `task next --json` when you need the next ready task in machine-readable form.

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
Status `last_output` is a low-token tail by default: the last 10 lines capped to 1200 characters. The full stripped capture remains in the monitor log, and matching still uses the full `--lines` capture.
Monitor pane targets must be nonblank; internal wrapper ids are also rejected before status files are written.

## Large output review

Use main-agent capture for short output. For large or monitored output, inspect the capped status tail first with the latest available lightweight/mini model, or with the main model at low reasoning. Escalate to medium reasoning and full `log_path` or explicit `capture` only when the first pass reports `error`, `unclear`, or `needs_analysis`. For `progressing` or `complete`, keep reasoning low and return a concise conclusion. Require this structure:

```text
Can judge:
Key conclusion:
Important verbatim excerpts:
Errors or risks:
Recommended next action:
Uncertainty:
```

If the subagent says it cannot judge, inspect the relevant output directly in the main agent.

## Status review flow

1. `tmux_job.py` or `tmux_monitor.py` writes terminal status.
2. A visible manager pane can poll heartbeat/status files while the worker runs.
3. A resumed Codex session uses `task load --for-skill`, `job status`, `watch status`, or direct status/log paths to inspect prior work explicitly.
4. If dormant Codex wakeup is required, use the bridge workflow after the same-thread app-server PoC gate has passed.

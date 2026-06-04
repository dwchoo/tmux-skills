# Workflows and Features

This document describes the desired tmux-skills operating model. Use it when deciding which helper command or managed worker to use for a real task.

## Desired Operating Model

tmux-skills should make long-running terminal work observable, resumable, and safe across Codex sessions. The agent should avoid hidden shell watchers and ad hoc process state. Every delayed action should either be represented by a status record, a follow-up task, or a managed worker record.

The preferred flow is:

1. Identify or create a stable target pane.
2. Send work only when the pane is safe to receive input.
3. Record long-running work through status files.
4. Use managed workers for delayed checks or delayed next commands.
5. Use hooks and task records to resume context after compaction, restart, or user prompt submission.
6. Use Autopilot objectives plus a Codex Desktop heartbeat when dormant Codex should wake later and continue bounded repair work.
7. Validate worker lifecycle behavior with real tmux E2E scenarios before relying on it.

## Feature Map

| Feature | Purpose | Primary commands | Canonical docs |
| --- | --- | --- | --- |
| Pane discovery | Find stable pane ids instead of relying on visual pane positions. | `list`, `current`, `resolve` | `SKILL.md` |
| Safe send | Avoid injecting commands into busy panes or non-executable scripts by accident. | `send --require-idle-shell`, `send --strict-preflight`, `send --bash-if-not-executable` | `managed-workers.md` |
| Long-running jobs | Run work with command files, logs, status JSON, and optional follow-up tasks. | `run --command`, `run --next-instruction` | `SKILL.md`, `references/WORKFLOWS.md` |
| Pane monitor | Watch a pane until one match, idle-shell event, timeout, or stop signal writes terminal status. | `monitor --match-regex`, `monitor --idle-shell` | `references/WORKFLOWS.md` |
| Managed watch | Keep an active pane visible to hooks without raw shell watcher processes. | `watch`, `watch status`, `watch cancel` | `managed-workers.md` |
| Queue after idle | Submit the next command only after a busy pane returns to an idle shell. | `queue-after-idle` | `managed-workers.md` |
| Queue after status | Submit the next command after a status TSV reaches required rows and the target pane is idle. | `queue-after-status` | `managed-workers.md` |
| Autopilot objective | Track a goal across attempts so heartbeat-woken Codex can diagnose, repair, and rerun bounded tmux work. | `autopilot start`, `autopilot tick`, `autopilot rerun` | This document |
| Bridge wakeup | `.codex/tmux-skills`의 terminal event와 ready task를 path-only prompt로 지정 Codex thread에 알린다. | `bridge register`, `bridge start`, `bridge status`, `bridge cancel` | This document |
| Duplicate prevention | Prevent multiple Codex instances from creating the same active watcher or queue by default. | managed start commands, `--allow-duplicate`, `--replace` | `managed-workers.md` |
| Stale recovery | Mark dead or orphaned active worker records as stale without killing unrelated PIDs. | `job gc --stale`, `watch gc --stale` | `managed-workers.md` |
| Real-use validation | Prove behavior through public CLI subprocesses and real tmux sessions. | `scripts/e2e_real_use.py` | `real-use-e2e.md` |

## Workflow: Start Work in tmux

Use this when the user asks to run, monitor, or inspect terminal work.

1. Run `python scripts/tmux_control.py current` if Codex is already inside tmux.
2. Run `python scripts/tmux_control.py list` when multiple sessions or panes may exist.
3. Resolve a human target with `resolve` before sending commands.
4. If no suitable pane exists, create one with `spawn` or `new-window`.
5. Report the selected stable `pane_id` and, when relevant, the `attach_command`.

Expected outcome:

- Later commands use a stable `%pane_id`.
- The user can attach to or inspect the same pane.
- No user-owned tmux object is renamed, closed, or interrupted without an explicit request.

## Workflow: Send a One-off Command Safely

Use this when the command is short-lived or the user explicitly wants the command sent to a pane.

```bash
python scripts/tmux_control.py send --pane %1 --command 'python script.py' --enter --require-idle-shell
```

Safety expectations:

- Prefer `--require-idle-shell` for user-selected panes.
- If a direct `.sh` command is not executable, choose one of:
  - `bash path/to/script.sh`
  - `--bash-if-not-executable`
  - deliberately fix the executable bit
- Use `capture` after execution before diagnosing failures or claiming completion.

## Workflow: Run Long Work with Resume Context

Use this when work may outlive the current turn or needs a follow-up instruction.

```bash
python scripts/tmux_control.py run \
  --pane %1 \
  --command 'python train.py' \
  --name training \
  --next-instruction 'Inspect the training result and choose the next experiment.'
```

Use `--next-instruction-file PATH` for longer follow-up text; the path and file content must be nonblank. Use `--next-on succeeded|failed|terminal` when the task should become ready on a different terminal result. `failed` matches unsuccessful terminal states: `failed`, `timeout`, `stopped`, `cancelled`, and `stale`; use `terminal` when any terminal state should wake the task.

Expected records:

- Command file under `.codex/tmux-skills/commands`.
- Log file under `.codex/tmux-skills/logs`.
- Status JSON under `.codex/tmux-skills/status`.
- Optional follow-up task under `.codex/tmux-skills/tasks`.
- If the wrapper receives SIGINT or SIGTERM, it stops the child process group and records terminal status `stopped`.
- If the wrapper command cannot be sent to the pane, the job records `failed`; follow-up tasks configured for `failed` or `terminal` become ready, while `succeeded` follow-ups are cancelled.

Resume behavior:

- Hooks can surface terminal events or ready tasks.
- A fresh session should inspect tasks explicitly with `task load --for-skill`; the report includes ready, running, blocked, and stale work. Running managed entries include their kind and pane.
- Follow-up tasks do not execute while Codex is absent.
- Task JSON files store canonical task fields only; derived fields such as `effective_status`, `matched_status`, and `stale` are read-time output.
- Text task summaries label stale in-progress tasks as `stale`; JSON output keeps the stored status plus the derived `stale` flag.

## Workflow: Run a Heartbeat Autopilot Objective

Use this when the user wants Codex to keep pursuing a long-running tmux goal after the current turn becomes dormant. Autopilot does not wake Codex by itself; create a Codex Desktop heartbeat for the current thread with the prompt generated by `heartbeat-prompt`.

```bash
python scripts/tmux_control.py autopilot start \
  --objective-id train-model \
  --pane %1 \
  --command 'python train.py' \
  --goal 'Finish model training; if it fails, diagnose, make a bounded repair, and rerun.'
```

Expected lifecycle:

1. `start` snapshots the command text, creates `.codex/tmux-skills/objectives/train-model.json`, and starts job `train-model-attempt-1` through `run`.
2. A heartbeat-woken Codex first runs `autopilot tick --objective-id train-model --for-agent --max-chars 1200`.
3. If the attempt is still running, tick returns `no_action`.
4. If the attempt succeeded, tick marks the objective `succeeded`.
5. If the attempt failed or stopped, tick leases the repair and returns `repair` with evidence paths and bounded repair policy.
6. After a bounded fix, Codex runs `autopilot rerun --objective-id train-model` to start the next attempt.
7. If `rerun` cannot send to the pane and attempts remain, it returns `rerun_failed` and keeps the objective repairable with the failed attempt as evidence.
8. If repair is unsafe or attempts are exhausted, Codex runs `autopilot block --objective-id train-model --reason TEXT`.

`tick --for-agent --json` includes `agent_instruction`, `attempt_summary`, evidence paths, safe follow-up commands, and the bounded repair policy. The summary starts small so routine heartbeats do not rehydrate full logs.

If `attempt_summary` is insufficient to diagnose a repair, expand evidence explicitly:

```bash
python scripts/tmux_control.py autopilot evidence --objective-id train-model --kind status --max-chars 8000
python scripts/tmux_control.py autopilot evidence --objective-id train-model --kind log --max-chars 8000
```

`autopilot evidence` returns JSON metadata plus bounded content. Missing or unreadable known artifacts are reported as JSON instead of a traceback. Increase `--max-chars` only when the bounded evidence is still insufficient; full log dumps should be a last resort.

Create the heartbeat prompt from the objective:

```bash
python scripts/tmux_control.py autopilot heartbeat-prompt --objective-id train-model
```

Bounded repair policy:

- Allowed: inspect logs/status, edit workspace code or config, run focused tests/checks, and rerun the objective command.
- Blocked without user approval: destructive cleanup, force git operations, push/deploy, dependency installation, secrets/auth changes, and intentionally expanding to higher-cost or longer training.
- A pane send failure is evidence for the next repair step; fix target pane selection or block explicitly instead of silently treating the objective as complete.
- Context policy: use `attempt_summary` first, bounded `autopilot evidence` second, and only expand beyond the default when the available evidence is insufficient.
- Completion, blocking, or cancellation does not delete the Codex Desktop heartbeat automatically; report that the heartbeat can be paused or removed.

## Workflow: Bridge Wakeup for Dormant Codex

`tmux-control bridge`는 `.codex/tmux-skills/status`의 terminal event와 `.codex/tmux-skills/tasks`의 ready task를 관찰하고, 같은 local `codex app-server`에 연결된 사용자가 지정한 main Codex thread에 wake prompt만 제출한다. bridge는 hook replacement가 아니다. Hooks는 active turn continuation 또는 resume/compact context를 돕고, bridge는 dormant CLI가 같은 app-server/thread에서 새 user turn을 받도록 깨우는 companion이다.

PoC hard gate가 먼저다. `scripts/codex_app_server_client.py`와 `scripts/tmux_bridge.py poc`가 live `codex app-server --listen unix://PATH` plus `codex --remote unix://PATH` 환경에서 same-thread wake를 증명하고 protocol fixture를 저장하기 전에는 daemon/background/cancel wiring을 구현하거나 운영에 사용하지 않는다.

Register and start:

```bash
python3 scripts/tmux_control.py bridge register \
  --thread-id THREAD \
  --endpoint unix://"$PWD/.codex/tmux-skills/bridge/sockets/codex-bridge.sock" \
  --workspace "$PWD"

python3 scripts/tmux_control.py bridge start \
  --bridge-id bridge-THREAD \
  --workspace "$PWD"
```

Status and cancel:

```bash
python3 scripts/tmux_control.py bridge status \
  --bridge-id bridge-THREAD \
  --workspace "$PWD" \
  --json

python3 scripts/tmux_control.py bridge cancel \
  --bridge-id bridge-THREAD \
  --workspace "$PWD"
```

PoC gate commands:

```bash
mkdir -p "$PWD/.codex/tmux-skills/bridge/sockets"
codex app-server --listen unix://"$PWD/.codex/tmux-skills/bridge/sockets/codex-bridge.sock"
codex --remote unix://"$PWD/.codex/tmux-skills/bridge/sockets/codex-bridge.sock" -C "$PWD"
python3 scripts/tmux_bridge.py poc \
  --thread-id THREAD \
  --endpoint unix://PATH \
  --workspace "$PWD" \
  --prompt "tmux-control observed a terminal event."
python3 scripts/tmux_bridge.py validate-poc \
  --runtime-json .codex/tmux-skills/bridge/poc-YYYYMMDD-HHMMSS.json
```

State and artifacts:

- Bridge records live under `.codex/tmux-skills/bridge/<bridge_id>.json`.
- Bridge locks live under `.codex/tmux-skills/bridge/<bridge_id>.lock`.
- PoC runtime evidence lives under `.codex/tmux-skills/bridge/poc-YYYYMMDD-HHMMSS.json`.
- PoC protocol fixtures live under `tests/fixtures/app_server_unix_ws/poc-YYYYMMDD-HHMMSS.json`.
- Manual same-thread confirmation notes live under `.codex/tmux-skills/bridge/poc-YYYYMMDD-HHMMSS.manual.md`.

Wake prompt shape is fixed and path-only:

```text
tmux-control observed a terminal event.

Workspace: <workspace>
Job ID: <job_id or unknown>
Status path: <status_path or none>
Task path: <task_path or none>
Log path: <log_path or none>

Please use $tmux-control to inspect the status and logs, then continue the requested work.
```

Ready task prompts use `tmux-control observed a ready task.` as the first line. The prompt must not include status/log summaries, `last_output`, stdout/stderr tails, traceback text, task instruction bodies, diagnosis, suggested commands, retry commands, or model/delegation language.

Safety rules:

- Endpoint v1 accepts only explicit `unix://PATH`.
- The bridge uses existing local Codex auth/session through local app-server Unix socket WebSocket transport; it does not call the OpenAI API directly and does not require `OPENAI_API_KEY`.
- The bridge does not discover threads. The operator supplies the target main thread id.
- The bridge does not send keys to tmux panes, run `codex app-server proxy`, run `codex exec resume`, use standalone `codex`, call `turn/steer`, call `thread/shellCommand`, or execute queued repair commands.
- Delivery failure is recorded in bridge state. Retryable failures leave the event unobserved; permanent failures mark the bridge failed.

## Workflow: Queue a Command After a Busy Pane Becomes Idle

Use this when a pane is busy now but should receive the next command later.

```bash
python scripts/tmux_control.py queue-after-idle \
  --job-id next-train \
  --pane %1 \
  --command 'python train_next.py' \
  --poll-seconds 2
```

Expected lifecycle:

1. Worker record starts under `.codex/tmux-skills/jobs`.
2. The worker heartbeats while the pane is busy.
3. Once the pane is idle, the command is submitted.

Use `--command-file PATH` instead of `--command TEXT` for long or multi-line queued commands.

Use `job cancel --job-id next-train` if the queued command is no longer wanted.

See [`managed-workers.md`](managed-workers.md) for exact states and duplicate behavior.

## Workflow: Queue a Command After Status Rows Are Ready

Use this when upstream work writes a status TSV and the next command should run only after required rows are satisfied.

```bash
python scripts/tmux_control.py queue-after-status \
  --job-id after-msec \
  --status-file logs/msec.status.tsv \
  --require-row 'run_cfg=configs/msec.toml,status=done' \
  --pane %1 \
  --command 'python train_lolv2.py'
```

Expected lifecycle:

1. The worker reads the TSV repeatedly until required rows match.
2. If required rows match but the pane is busy, the worker waits for idle.
3. If a fail row matches, the worker does not submit the command.
4. If required rows match and the pane is idle, the worker submits the command.

Prefer exact header TSV assignment specs such as `run_cfg=configs/msec.toml,status=done`. See [`managed-workers.md`](managed-workers.md) for matching rules and compatibility formats.

## Workflow: Keep a Watch Visible to Hooks

Use this when a pane or status file should remain visible in hook context.

```bash
python scripts/tmux_control.py watch \
  --job-id train-watch \
  --pane %1 \
  --interval 180 \
  --capture-lines 80
```

Expected behavior:

- The active watch appears in hook context as a managed job.
- The worker writes a full stripped pane capture to the log on each heartbeat.
- The status `last_output` is a low-token tail by default: the last 10 lines capped to 1200 characters.
- Timeout or cancellation is recorded for later inspection.
- Use the status tail for low-reasoning triage, then inspect the full log only for errors, unclear output, or analysis-heavy results.

For status-file-first monitoring, use low-token mode:

```bash
python scripts/tmux_control.py watch \
  --job-id train-watch-low-token \
  --pane %1 \
  --status-file logs/train.status.tsv \
  --low-token
```

Expected low-token behavior:

- `--status-file` is required.
- Normal heartbeats read the status file and do not capture the pane.
- If the status file is missing, the watch remains running and records a compact reason instead of falling back to pane capture.

## Workflow: Prevent Duplicate Managed Work

This is the default behavior when multiple Codex instances share a workspace.

Expected behavior:

- Same active dedupe input is rejected by default.
- Use `--allow-duplicate` only when parallel workers are intentional.
- Use `--replace` only when intentionally replacing the same managed job.

See [`managed-workers.md`](managed-workers.md) for exact duplicate JSON fields, exit codes, owner behavior, and replace semantics.

## Workflow: Recover from Stale or Unsafe Worker Records

Use this when a worker record looks active but the real worker is gone or no longer trustworthy.

```bash
python scripts/tmux_control.py job gc --stale --dry-run --compact
python scripts/tmux_control.py job gc --stale
python scripts/tmux_control.py watch gc --stale --dry-run --compact
```

Expected behavior:

- Logs and status files are kept as evidence.
- A live PID that does not look like the managed worker is not killed.
- Active records with dead or foreign PIDs are reported with read-time `effective_status` and can be marked stale by GC.
- After stale GC, the same dedupe input can be recreated.

See [`managed-workers.md`](managed-workers.md) for the stale threshold and exact GC contract.

Use compact output when Codex should inspect worker state without rehydrating verbose payloads:

```bash
python scripts/tmux_control.py watch list --compact --no-observed-tail --max-chars 400
python scripts/tmux_control.py job status --job-id train-watch --compact --include-pane-state
```

## Workflow-to-Test Mapping

| Workflow or feature | E2E scenario coverage |
| --- | --- |
| Queue after idle on an idle pane | `idle-continuation` |
| Queue after status only after required TSV rows | `status-chain` |
| Status-ready command waits for busy pane idle | `status-chain-waits-for-busy-pane` |
| Concurrent duplicate prevention across two CLI starts | `concurrent-duplicate-race` |
| Sequential duplicate rejection | `duplicate-block` |
| Strict script preflight avoids accidental send | `preflight-strict` |
| Active watch appears in hook context | `watch-visibility` |
| Busy pane queue does not submit prematurely | `busy-pane-wait` |
| Queued command files are copied and submitted | `queue-command-file` |
| Fail rows block queue submission | `status-fail-blocks` |
| Intentional parallel duplicate metadata | `allow-duplicate` |
| Watch duplicate behavior matches queue duplicate behavior | `watch-duplicate-block` |
| Same job replacement and different job duplicate rejection | `replace-same-job-only` |
| User cancellation stops active queue before submission | `cancel-active-queue` |
| Stale records can be marked and recreated | `stale-gc-recovery` |
| Foreign live PIDs are not killed by replace | `replace-rejects-foreign-pid` |
| Missing target pane fails terminally without submission | `pane-missing-failure` |

Use `docs/real-use-e2e.md` for the detailed scenario setup, action, and assertion matrix.

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
6. Validate worker lifecycle behavior with real tmux E2E scenarios before relying on it.

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
| Duplicate prevention | Prevent multiple Codex instances from creating the same active watcher or queue by default. | managed start commands, `--allow-duplicate`, `--replace` | `managed-workers.md` |
| Stale recovery | Mark orphaned active worker records as stale without killing unrelated PIDs. | `job gc --stale` | `managed-workers.md` |
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
python scripts/tmux_control.py job gc --stale --dry-run
python scripts/tmux_control.py job gc --stale
```

Expected behavior:

- Logs and status files are kept as evidence.
- A live PID that does not look like the managed worker is not killed.
- After stale GC, the same dedupe input can be recreated.

See [`managed-workers.md`](managed-workers.md) for the stale threshold and exact GC contract.

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

# Managed Workers

This document is the canonical reference for managed `watch`, `queue-after-idle`, and `queue-after-status` behavior.

## Overview

Managed workers replace raw shell `sleep` watchers when a later action must be visible, deduplicated, cancellable, and recorded. Records live under `.codex/tmux-skills/jobs`, with status and logs under `.codex/tmux-skills/status` and `.codex/tmux-skills/logs`.

Use managed workers when:

- A pane should be watched repeatedly.
- A command should run after a pane returns to an idle shell.
- A command should run after a status TSV reaches required row states.
- Multiple Codex instances may share the same workspace and should not create duplicate active workers by default.

## Commands

```bash
python3 scripts/tmux_control.py watch --job-id msec-monitor --pane %91 --interval 180 --capture-lines 80
python3 scripts/tmux_control.py watch --job-id msec-monitor --pane %91 --status-file logs/msec.status.tsv --low-token
python3 scripts/tmux_control.py watch list --compact --no-observed-tail
python3 scripts/tmux_control.py watch status --job-id msec-monitor --compact
python3 scripts/tmux_control.py watch cancel --job-id msec-monitor

python3 scripts/tmux_control.py queue-after-idle --job-id lolv2-after-msec --pane %91 --command 'bash scripts/train_lolv2.sh'
python3 scripts/tmux_control.py queue-after-status --job-id lolv2-after-msec --status-file logs/msec.status.tsv --require-row 'run_cfg=configs/msec.toml,status=done' --pane %91 --command 'bash scripts/train_lolv2.sh'
python3 scripts/tmux_control.py queue-after-status --job-id lolv2-after-msec --status-file logs/msec.status.tsv --require-row 'run_cfg=configs/msec.toml,status=done' --pane %91 --command 'bash scripts/train_lolv2.sh' --low-token
python3 scripts/tmux_control.py queue-after-idle --job-id next-from-file --pane %91 --command-file scripts/next-command.sh
python3 scripts/tmux_control.py job status --job-id lolv2-after-msec --compact --include-pane-state
python3 scripts/tmux_control.py job cancel --job-id lolv2-after-msec
python3 scripts/tmux_control.py job gc --stale --dry-run
python3 scripts/tmux_control.py watch gc --stale --dry-run
```

Queue commands accept report-friendly aliases:

- `--then-pane` for `--pane`
- `--then-command` for `--command`
- `--interval` for `--poll-seconds`
- `--then-require-idle-shell` for the default idle-shell guard
- `queue-after-status --no-require-idle-shell` to submit immediately once status rows match, without waiting for the pane to look idle

Polling intervals and timeouts must be finite positive numbers; zero, negative, `NaN`, and infinity values are rejected before a worker starts.
`watch --status-lines` and `watch --status-max-chars` must be finite positive integers. They control only status `last_output`, defaulting to the last 10 lines capped to 1200 characters. `--capture-lines` still controls the full pane capture written to the log.
`watch --low-token` requires `--status-file`. In low-token mode, normal heartbeats read only that status file, write a bounded status summary, and do not capture the pane unless the worker hits an error path.
Queue command sources must be nonblank; empty `--command`, whitespace-only command files, and blank `--command-file` paths fail before a worker starts.
`watch --status-file` and `queue-after-status --status-file` paths must be nonblank when provided. `queue-after-status` also requires nonblank `--require-row`/`--fail-row` values.
Managed workers and `job status`/`job cancel` require nonblank `--job-id` values; worker starts also require nonblank `--pane` values. Blank identifiers are never normalized to a default job record.
`job list|status` and `watch list|status` accept `--compact`, `--no-observed-tail`, and `--max-chars N`. `--compact` omits verbose fields such as `argv` and `dedupe_payload`; `--max-chars` truncates string fields rather than truncating the whole JSON document.
`job status|cancel|gc` and `watch status|cancel|gc` accept `--include-pane-state` to report whether the target pane still exists. This never closes panes or windows.

## Dedupe Contract

Managed workers use a canonical `dedupe_key` stored in `.codex/tmux-skills/jobs`. Active jobs with the same key are rejected by default, even when they have different owners.

Duplicate reject stdout JSON includes:

- `started: false`
- `duplicate: true`
- `dedupe_key`
- `existing_job_id`
- `existing`
- `reason`

Duplicate reject exits with status `2`.

Use `--allow-duplicate` only for intentional parallel workers. The new record keeps the same `dedupe_key` and records `duplicate_allowed: true` plus `duplicate_of: <job_id>`.

Use `--replace` only to replace an active managed worker with the same `--job-id`. It signals only a live PID whose command line still matches the expected tmux-skills worker, waits briefly for it to exit, and does not start the replacement if the old worker cannot be stopped safely.

Dead or orphaned records with the same `dedupe_key` do not block a new worker. Before the new worker starts, tmux-skills marks those records `stale` and records `replaced_by`.

`owner` is metadata only. It is not used for permission checks.

## State Model

Active managed states:

- `starting`
- `running`
- `waiting`
- `waiting_status`
- `waiting_pane_idle`

Terminal managed states:

- `submitted`
- `failed`
- `timeout`
- `cancelled`
- `stale`

These lists are stored status values. `submitted` is terminal and is not a duplicate blocker. It remains as execution evidence only.

Read-time `job list`, `job status`, `watch list`, and `watch status` add process fields without rewriting the stored record:

- `effective_status`: the stored terminal status, a verified active status, `dead`, or `orphaned`
- `process_state`: `verified_worker`, `dead_pid`, `missing_pid`, `foreign_pid`, `starting`, `terminal`, or `unknown`
- `pid_running`
- `pid_matches`
- `stale_reason`

Stored terminal statuses take precedence. Fresh `starting` records with no PID are treated as a short-lived startup placeholder. Other active records with no PID or a dead PID are reported immediately as `effective_status=dead`; active records whose PID is live but no longer matches the tmux-skills worker command are reported as `effective_status=orphaned`.

## Stale GC

Use `job gc --stale --dry-run` to inspect dead, orphaned, or heartbeat-stale active records, then `job gc --stale` to mark them stale. Use `watch gc --stale --dry-run` for the same operation filtered to watch records. GC marks records and status files as `stale`; it does not delete logs, status files, or command files.

A stale GC candidate is an active record whose worker process is dead, missing, foreign, or whose heartbeat age is at least `max(3 * check_interval_seconds, 300)` seconds and whose recorded PID is missing or no longer looks like a managed `tmux_queue.py queue-after-idle|queue-after-status|watch --job-id <id>` worker with an exact job-id argument.

If a PID is alive but its command line does not match the managed worker, tmux-skills records stale or blocked evidence and does not kill the process.

## Pane and Window Cleanup

Managed worker `pane_id` refers to the target pane that is watched or receives queued commands. It is not proof that tmux-skills created the pane or window. For that reason, cancellation, replacement, and GC never close panes or windows by default.

Use `--include-pane-state` on status, cancel, or GC commands when you need a lightweight report showing whether the target pane still exists.

## Send Preflight

`send` performs a lightweight preflight for direct script commands such as `./scripts/foo.sh`; when no explicit cwd is supplied by a caller, it resolves relative script paths against the target pane's current path. Managed queue submissions use the same target-pane context because the command is sent into that pane.

```bash
python3 scripts/tmux_control.py send --pane %91 --command './scripts/foo.sh' --enter --strict-preflight
python3 scripts/tmux_control.py send --pane %91 --command './scripts/foo.sh' --enter --bash-if-not-executable
```

- Default behavior is warning-only.
- `--strict-preflight` refuses to send a non-executable `.sh` script and returns JSON with `sent_to_pane: false`.
- `--bash-if-not-executable` rewrites the command to `bash path/to/script.sh ...`.

## Failure Rules

- Queue workers wait for an idle shell before submitting commands unless configured otherwise; `queue-after-status --no-require-idle-shell` is the explicit bypass.
- `queue-after-status` supports exact header TSV assignment specs such as `run_cfg=configs/msec.toml,status=done`.
- `KEY:VALUE` specs match exact tab-separated fields; specs without `:` or `=` use substring matching for backward compatibility.
- A fail row match marks the job `failed` and does not submit the next command.
- If the target pane is missing, the worker records `failed` and does not submit the next command.
- Cancelled managed workers record `cancelled` and should not submit pending commands.

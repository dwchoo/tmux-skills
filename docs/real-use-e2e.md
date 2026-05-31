# Real-use E2E

This document is the canonical reference for `scripts/e2e_real_use.py`.

## Overview

The real-use E2E harness validates managed worker behavior through the public `scripts/tmux_control.py` CLI and real tmux worker processes. It uses a temporary workspace, a temporary `TMUX_TMPDIR`, and an isolated tmux session. It does not target user tmux sessions or repository runtime state.

## Commands

```bash
python3 scripts/e2e_real_use.py --scenario smoke
python3 scripts/e2e_real_use.py --scenario all --json
python3 scripts/e2e_real_use.py --scenario concurrent-duplicate-race --json
python3 scripts/e2e_real_use.py --scenario idle-continuation --keep-artifacts
python3 scripts/e2e_real_use.py --scenario all --keep-going --json
```

Options:

- `--scenario smoke` runs the fast real-use set by default.
- `--scenario all` runs smoke plus full-only lifecycle scenarios.
- `--scenario <name>` runs one named scenario.
- `--json` prints a machine-readable summary.
- `--keep-artifacts` keeps the temporary workspace after a failure for debugging.
- `--keep-going` continues after scenario-body failures and aggregates results. Teardown, isolation, or cleanup failures still stop the run because later scenarios may no longer be isolated.

## Scenario Groups

Current code facts from `scripts/e2e_real_use.py`:

- `smoke` has 7 scenarios.
- `all` has 24 scenarios.
- `all` is `smoke` plus the full-only scenarios.

Smoke scenarios:

- `idle-continuation`
- `status-chain`
- `status-chain-waits-for-busy-pane`
- `concurrent-duplicate-race`
- `preflight-strict`
- `watch-visibility`
- `capture-strips-ansi`

Full-only scenarios:

- `busy-pane-wait`
- `queue-command-file`
- `status-fail-blocks`
- `duplicate-block`
- `allow-duplicate`
- `watch-duplicate-block`
- `watch-concurrent-race`
- `replace-same-job-only`
- `cancel-active-queue`
- `stale-gc-recovery`
- `corrupted-state-degrades`
- `replace-rejects-foreign-pid`
- `pane-missing-failure`
- `status-timeout-blocks`
- `pane-dies-mid-wait`
- `task-followup-flow`
- `stop-hook-blocks-terminal`

## Scenario Matrix

Each harness run uses a temporary workspace, a temporary `TMUX_TMPDIR`, and an isolated tmux session. Scenarios stay independent through unique job ids plus pre/post scenario cancellation and pane interruption. The harness calls `scripts/tmux_control.py` through subprocesses instead of importing internal functions.

| Scenario | Workflow covered | Setup | Action | Required assertions |
| --- | --- | --- | --- | --- |
| `idle-continuation` | Queue a command after an already idle pane. | Start an isolated tmux pane and choose an output file. | Start `queue-after-idle` with a short `printf` command. | Job reaches `submitted`, output file is written, and the record includes `dedupe_key`, `owner`, and `check_interval_seconds`. |
| `status-chain` | Queue a command after a status TSV reaches required rows. | Write a TSV with `running` status and no output file. | Start `queue-after-status`, verify no early output, then rewrite TSV to `done`. | Command is not submitted while status is `running`; after `done`, job reaches `submitted` and output file contains the expected text. |
| `status-chain-waits-for-busy-pane` | Status-ready command waits for pane idle. | Put the pane in `sleep`, write the status TSV as already `done`, and choose an output file. | Start `queue-after-status` while the pane is busy. | Job reaches `waiting_pane_idle`, output file does not exist during sleep, then job reaches `submitted` after idle. |
| `concurrent-duplicate-race` | Registry lock and dedupe under simultaneous CLI starts. | Put the pane in `sleep` so both queues remain active long enough to race. | Launch two `queue-after-idle` CLI subprocesses with the same pane and command but different job ids and owners. | Exactly one process returns `started: true`; exactly one exits `2` with `duplicate: true`; only one active record exists for the dedupe key. |
| `duplicate-block` | Sequential duplicate reject compatibility path. | Put the pane in `sleep` and start a first active queue with owner A. | Start a second same-dedupe queue with owner B. | Second start exits `2`, JSON has `duplicate: true`, `existing_job_id` points to the first job, and the duplicate command must not submit. |
| `preflight-strict` | Strict send preflight. | Create a non-executable `.sh` script that would write an output file if executed. | Call `send --strict-preflight --enter` with the script path. | Command exits `2`, JSON reports `sent_to_pane: false`, and the output file is absent. |
| `watch-visibility` | Managed watch appears in hook context. | Start an isolated pane and a short-timeout `watch`. | Poll `codex_tmux_hook.py context` for active managed job text. | Hook context includes `managed job <id>: running`; after timeout, job reaches `timeout` and the watch log exists. |
| `capture-strips-ansi` | Capture strips terminal control noise. | Start an idle isolated pane. | Send a command that prints ANSI color and carriage-return progress noise, then run `capture --strip-ansi`. | Stripped capture contains visible output such as `DONE`, contains no ESC byte or raw CSI sequence, and plain capture remains callable. |
| `busy-pane-wait` | Queue after idle does not submit prematurely. | Put the pane in `sleep` and choose an output file. | Start `queue-after-idle` while the pane is busy. | Output file is absent during sleep; after idle, job reaches `submitted` and output is written. |
| `queue-command-file` | Queue command-file submission. | Write a command file in the temporary workspace and choose an output file. | Start `queue-after-idle` with `--command-file`. | Job reaches `submitted`, output file is written, and the command file is copied into managed state before the worker runs it. |
| `status-fail-blocks` | Fail rows block queued submission. | Write a TSV with `running` status and configure both require and fail rows. | Start `queue-after-status`, then rewrite TSV to the fail status. | Job reaches `failed`, matched fail rows are recorded, and the command output file is absent. |
| `allow-duplicate` | Intentional duplicate escape hatch. | Put the pane in `sleep` and start a first active queue. | Start a second queue with the same dedupe input plus `--allow-duplicate`. | Second job starts and its record contains `duplicate_allowed: true` and `duplicate_of` pointing to the first job. |
| `watch-duplicate-block` | Watch dedupe contract. | Create a status file and start a first watch for the same pane/status-file pair. | Start a second watch without `--allow-duplicate`, then a third with `--allow-duplicate`. | Second watch exits `2` with `duplicate: true`; allowed watch starts and records duplicate metadata. |
| `watch-concurrent-race` | Watch registry lock and dedupe under simultaneous CLI starts. | Start an idle isolated pane and choose two watch job ids with the same pane and no status file. | Launch two `watch` CLI subprocesses concurrently with different owners and a short timeout. | Exactly one process returns `started: true`; exactly one exits `2` with `duplicate: true`; only one active watch record exists for the dedupe key. |
| `replace-same-job-only` | Replace semantics. | Start a queue with one job id, then start another queue with the same job id and `--replace`. | Wait for the replacement command to submit, then attempt `--replace` using a different job id but same dedupe input. | Same job id replacement runs the second command; different job id is duplicate-rejected with exit `2`. |
| `cancel-active-queue` | User cancellation before delayed submission. | Put the pane in `sleep`, start a queue, and wait for `waiting_pane_idle`. | Call `job cancel --job-id <id>`. | Job reaches `cancelled`, `pid_running` becomes false, and the queued command output file is absent. |
| `stale-gc-recovery` | Stale record marking and dedupe reuse. | Create a submitted seed record, then write a fake old active record with the same dedupe key and stale timestamps. | Run `job gc --stale --dry-run`, then `job gc --stale`, then recreate the same queue. | Dry run reports the stale job, GC marks it `stale`, and a new job with the same dedupe input can start. |
| `corrupted-state-degrades` | Reader CLIs tolerate unreadable state files. | Run a quick successful job, create one valid managed queue record, then write malformed or non-UTF-8 JSON into the workspace status and jobs state directories. | Call `job list` and `codex_tmux_hook.py context` while corrupted files are present. | Both calls exit `0` without a Python traceback, the valid managed job remains visible in `job list`, and hook context reports skipped unreadable state files. |
| `replace-rejects-foreign-pid` | Foreign PID safety. | Start a live non-`tmux_queue.py` process and write a managed job record pointing at its PID. | Start the same job id with `--replace`. | Command exits `2`, reason says the PID no longer looks like this tmux-skills worker, and the foreign process remains alive until harness cleanup. |
| `pane-missing-failure` | Missing pane terminal failure. | Use a pane id that should not resolve. | Start `queue-after-idle` targeting the missing pane. | Job reaches `failed` quickly and the queued command output file is absent. |
| `status-timeout-blocks` | Status wait timeout does not submit. | Write a status TSV that stays in a non-matching state and choose an output file. | Start `queue-after-status` with an unsatisfied `--require-row`, short polling, and a one-second timeout. | Job reaches `timeout`, output file is absent, status diagnostics preserve `matched_required_rows`, and no send result is recorded. |
| `pane-dies-mid-wait` | Queue wait handles a pane disappearing. | Put the target pane in `sleep` and choose an output file. | Start `queue-after-idle`, wait for `waiting_pane_idle`, then kill the target pane. | Job reaches a terminal failure state instead of hanging, no send result is recorded, output file is absent, and the harness recreates the isolated pane for later scenarios. |
| `task-followup-flow` | Run-created follow-up tasks become ready and claimable. | Start with an idle pane. | Run a quick successful job with `--next-instruction` and `--next-on succeeded`, then poll task APIs. | The task becomes `ready`, hook context exposes the ready task and instruction, `task claim` succeeds, and `task next --json` no longer returns that task as ready. |
| `stop-hook-blocks-terminal` | Stop hook blocks once for terminal events. | Drain previous terminal stop notifications, then run a quick job without follow-up task creation. | Call `codex_tmux_hook.py stop --workspace <workspace>` twice with stdin `{}`. | First call returns `decision: block` with a terminal-event reason; second identical call returns no block because the event was acknowledged. |

## Scenario Design Rules

- Prefer public CLI subprocesses over internal Python calls.
- Bound each harness subprocess call so a stuck helper reports a scenario failure instead of hanging the whole E2E run.
- Keep each scenario independent with unique job ids, bounded subprocesses, active job cancellation, and pane interruption.
- Make negative assertions explicit with consistent language: command must not submit, or output file is absent.
- Verify state transitions, not just final files.
- Use `smoke` for high-risk everyday flows and `all` for lifecycle, recovery, and safety edges.
- If a scenario is intentionally excluded from `smoke` or `all`, document that exclusion explicitly.
- Keep scenario names stable because docs, JSON output, and developer workflows reference them directly.

## Exit Codes

- `0`: all selected scenarios passed.
- `1`: at least one selected scenario failed.
- `77`: `tmux` is not installed or is not on `PATH`; treat as skipped.

## Diagnostics

On failure, the harness reports:

- Failing scenario and step.
- Command arguments, stdout, stderr, return code, and parsed JSON when available.
- Temporary workspace and state directory.
- Isolated tmux session and pane id.
- Recent pane capture.
- Bounded state tree summary.
- Managed job status details for jobs touched by the scenario.
- If diagnostics collection itself fails, the original failure is preserved and the failure JSON includes `diagnostics_error`.
- If final cleanup raises an exception, the summary records an `e2e-cleanup-verification` failure with `cleanup_error`.

## Cleanup Verification

Each scenario starts and ends with best-effort active job cancellation and pane interruption so one scenario does not leak work into the next.
After each cancellation pass, the harness keeps only jobs still reported active in its internal tracking list so terminal jobs are not cancelled again during later scenario teardown.

On success, the harness verifies:

- The isolated tmux session is gone.
- The isolated tmux server has been stopped.
- Workspace-scoped detached `tmux_queue.py` workers have been signalled if any survived tmux teardown.
- The temporary directory was removed.
- The repository has no `__pycache__` or `.pyc` artifacts.

The harness removes Python runtime artifacts before cleanup verification. When `--keep-artifacts` is used, artifacts are kept only after failure.
The cleanup summary exposes `session_absent`, `server_absent`, `worker_pids_signalled`, `temp_dir_removed`, and repository runtime artifact fields.

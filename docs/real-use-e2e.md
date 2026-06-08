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
- `all` has 35 scenarios.
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
- `autopilot-repair-rerun`
- `manager-visible-success`
- `manager-visible-failure`
- `manager-run-next`
- `manager-multi-pane`
- `manager-tui-delete-completed`
- `manager-bridge-random-notify`
- `manager-tmux-inject-wakes-current-codex`
- `manager-random-repeat-until-zero-one`
- `manager-start-reuses-live-process`
- `manager-cancel`
- `manager-process-exit-keeps-worker`

## Core Demo Boundary

현재 핵심 데모는 visible manager가 현재 tmux session에서 background process로 유지되고, worker pane에서 15초 tick 출력 후 random digit을 종료하며, `tmux-inject` wake prompt가 bound Codex pane으로 전달되고, Codex가 manager state를 확인한 뒤 `manager ack`를 기록하는 흐름이다. `tmux-inject` wake prompt는 `ID:<six-hex-wake-id>;` 첫 줄을 사용하고, stale/handled wake id가 화면에 남아 있어도 최신 event injection을 막지 않아야 한다. 반복 데모는 첫 번째 `0` 또는 `1`은 제외하고 두 번째 `0` 또는 `1`이 나올 때까지 `manager submit`을 반복해야 하며, manager pane과 worker pane layout을 유지해야 한다.

이 데모에 직접 필요한 경로는 `manager start --process-mode background --notify tmux-inject`, `manager submit`, terminal event 기록, tmux-inject delivery check, event-scoped `manager.observe`, `manager ack`, manager cleanup/cancel safety다. Lifecycle hook은 기본 데모 경로에 필요하지 않은 optional fallback guard이며, 독립 `bridge register|start|status|cancel` daemon 경로, app-server PoC fixture, legacy state compatibility, watch/queue/autopilot 경로는 별도 시나리오가 요구할 때만 유지한다. 코드 정리는 이 core demo boundary를 깨지 않는 선에서 진행하고, 삭제 전에는 해당 항목이 이 표의 시나리오나 현재 문서 계약에 필요한지 확인한다.

`tmux-inject` delivery check는 bound Codex pane capture에서 `Working` 상태가 보이더라도 마지막 wake prompt 블록과 composer footer가 함께 남아 있으면 staged prompt로 판정해야 한다. Composer footer에는 `queue message`, `submit message`, context hint뿐 아니라 최신 Codex TUI의 `gpt-... · /path` model/status footer도 포함한다. 이 deterministic staged 판정은 sidecar의 `confirmed` 결정보다 우선하며, prompt가 남아 있으면 manager는 bounded submit 또는 queue follow-up을 실행한 뒤 다시 capture해야 한다. footer가 `queue message`이면 manager는 Enter 재전송이 아니라 bounded `Tab` follow-up을 사용해 현재 작업 뒤로 prompt를 queue해야 하며, ack가 기록되기 전까지 event를 receipt로 처리하지 않는다. Wake prompt 첫 줄은 `ID:<wake_id>;`이고, 같은 `wake_id`가 이미 Codex composer/queue에 보이면 manager는 재주입하지 않고 `queued_in_codex`로 기록해야 한다. 다른 active unacknowledged 또는 unknown wake id가 보이면 새 prompt를 넣지 않고 `blocked_by_other_wake` 또는 `deferred` 상태로 사용자의 TUI/CLI 선택을 기다리지만, acknowledged/handled wake id는 stale evidence로만 기록하고 최신 event를 막지 않는다. Codex는 manager event prompt를 받은 뒤 manager state를 1회 inspect하고, event-scoped one-time token으로 필요한 결과만 읽은 뒤 ack한다. stale/handled event는 report-only 처리하며, `manager run-next` 후에는 직접 worker pane이나 manager status를 polling하지 않고 다음 manager event를 기다려야 한다.

## Basic Demo Scenario

이 시나리오는 실제 사용자가 보는 기본 시연 흐름이다. Harness scenario는 아래 절차를 자동화해서 검증하되, 사람이 시연할 때도 같은 제약과 성공 기준을 따른다.

### Constraints

- 현재 tmux session에서 진행한다.
- 기존 manager가 있으면 `manager start`를 다시 실행해서 같은 workspace/window의 manager를 재사용하거나 새 작업을 그 manager에 붙인다.
- 완료 후 manager pane과 worker pane은 닫지 않는다.
- manager가 떠 있는 동안 tmux-skills로 실행하는 장기 스크립트는 직접 pane에 보내지 않고 manager를 통해 다른 worker pane에서 실행한다.

### Procedure

1. 현재 Codex pane을 기준으로 background process mode manager를 띄운다.

   ```bash
   python scripts/tmux_control.py current
   python scripts/tmux_control.py manager start \
     --process-mode background \
     --notify tmux-inject \
     --codex-pane current
   ```

2. manager가 살아 있으면 이후 장기 작업은 `manager submit`으로만 제출한다. 이 데모의 worker command는 attempt 번호를 출력하고, 1초 간격으로 15개 tick을 출력한 뒤 random digit `0`부터 `9` 중 하나를 출력하고 종료한다.

   ```bash
   python scripts/tmux_control.py manager submit \
     --job-id random-demo-1 \
     --command 'python3 -c "import random,time; attempt=1; print(f\"ATTEMPT={attempt}\", flush=True); [(print(f\"tick {i}/15\", flush=True), time.sleep(1)) for i in range(1,16)]; print(f\"RANDOM_DIGIT={random.randint(0,9)}\", flush=True)"'
   ```

3. worker command가 terminal 상태가 되면 manager가 bound Codex pane에 `ID:<wake_id>;`로 시작하는 tmux-inject wake prompt를 보낸다.

4. Codex는 wake prompt를 받은 뒤 `manager status`를 한 번만 inspect하고, 최신 unacknowledged terminal event를 확인한다. 필요한 결과는 event-scoped one-time token 또는 사용자가 명시적으로 요청한 `manager.observe` grant로만 읽는다.

5. Codex는 `manager ack --event-id EVENT_ID`를 기록한 뒤 사용자에게 `숫자는 N이 나왔습니다.`라고 보고한다. `pane_id`, `status_path`, `log_path`, raw status/log `cat`, 또는 `tmux capture`를 직접 사용하면 실패다.

6. 출력 숫자가 `0` 또는 `1`이 나올 때까지 Codex가 `manager submit`으로 다음 작업을 반복한다. 단, 첫 번째 시도에서 `0` 또는 `1`이 나오면 성공으로 세지 않고 최소 한 번 더 실행한다.

7. 반복 중에도 Codex는 worker pane을 직접 polling하지 않는다. 각 제출 후에는 다음 manager event를 기다리고, event를 받을 때마다 status inspect 1회, ack 1회, 결과 보고 또는 다음 `manager submit` 1회만 수행한다.

### Success Criteria

- manager는 Codex-owned background terminal running 상태로 유지되고, `/ps`에서 추적 가능한 manager process로 남는다.
- 작업은 현재 tmux session의 별도 worker pane에서 실행된다.
- 각 worker command는 `ATTEMPT=N`, `tick 1/15`부터 `tick 15/15`, `RANDOM_DIGIT=N` 형식의 output을 남기고 terminal event를 만든다.
- 각 terminal event는 tmux-inject notification, event-scoped one-time read 또는 명시 observe grant, Codex `manager ack`, 결과 보고 순서로 처리된다.
- Codex-facing output from `manager submit` 직후에는 worker `pane_id`, `status_path`, `log_path`가 없다.
- Event-scoped token은 해당 event read와 ack 뒤 재사용할 수 없다.
- Grant 없이 manager-owned raw output을 읽으려는 CLI/MCP/hook 경로는 redacted 또는 blocked 결과를 낸다.
- 최종 보고 숫자는 `0` 또는 `1`이고, attempt 1의 `0` 또는 `1`은 최종 성공으로 세지 않는다.
- 데모 종료 후 manager pane과 worker pane은 열린 상태로 유지된다.

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
| `watch-visibility` | Managed watch appears in status review. | Start an isolated pane and a short-timeout `watch`. | Poll `job list --compact` for the active managed job. | The active managed job is visible while running; after timeout, job reaches `timeout` and the watch log exists. |
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
| `corrupted-state-degrades` | Reader CLIs tolerate unreadable state files. | Run a quick successful job, create one valid managed queue record, then write malformed or non-UTF-8 JSON into the workspace status and jobs state directories. | Call `job list` and `task load --for-skill` while corrupted files are present. | Both calls exit `0` without a Python traceback, the valid managed job remains visible in `job list`, and task load reports skipped unreadable state files. |
| `replace-rejects-foreign-pid` | Foreign PID safety. | Start a live non-`tmux_queue.py` process and write a managed job record pointing at its PID. | Start the same job id with `--replace`. | Command exits `2`, reason says the PID no longer looks like this tmux-skills worker, and the foreign process remains alive until harness cleanup. |
| `pane-missing-failure` | Missing pane terminal failure. | Use a pane id that should not resolve. | Start `queue-after-idle` targeting the missing pane. | Job reaches `failed` quickly and the queued command output file is absent. |
| `status-timeout-blocks` | Status wait timeout does not submit. | Write a status TSV that stays in a non-matching state and choose an output file. | Start `queue-after-status` with an unsatisfied `--require-row`, short polling, and a one-second timeout. | Job reaches `timeout`, output file is absent, status diagnostics preserve `matched_required_rows`, and no send result is recorded. |
| `pane-dies-mid-wait` | Queue wait handles a pane disappearing. | Put the target pane in `sleep` and choose an output file. | Start `queue-after-idle`, wait for `waiting_pane_idle`, then kill the target pane. | Job reaches a terminal failure state instead of hanging, no send result is recorded, output file is absent, and the harness recreates the isolated pane for later scenarios. |
| `task-followup-flow` | Run-created follow-up tasks become ready and claimable. | Start with an idle pane. | Run a quick successful job with `--next-instruction` and `--next-on succeeded`, then poll task APIs. | The task becomes `ready`, `task load --for-skill` exposes the ready task and instruction, `task claim` succeeds, and `task next --json` no longer returns that task as ready. |
| `autopilot-repair-rerun` | Heartbeat-style Autopilot repair loop. | Start with an idle pane and choose an objective id. | Start an Autopilot objective whose first attempt fails with long output, call bounded `tick`, expand bounded log evidence, call duplicate `tick`, rerun with a fixed command, then tick again. | First tick returns `repair` with bounded `attempt_summary` and evidence commands, log evidence returns failure output without a full dump, duplicate tick returns `no_action`, rerun starts attempt 2, attempt 2 succeeds, final tick completes the objective without extra evidence commands, and heartbeat prompt includes the wake contract. |
| `manager-visible-success` | Codex-owned visible manager waits for review after success. | Start with an isolated tmux pane and choose an output file. | Start foreground `manager start --notify none` with a successful command. | A tall right-side worker pane and compact manager pane below Codex are created, one viewer process renders the compact summary, output is written, job status reaches `succeeded`, manager status remains `waiting_for_codex`, repeated `clear; cat` prompt history is not accumulated, and the manager process can be cancelled without stopping the worker result. |
| `manager-visible-failure` | Codex-owned visible manager waits for review after failure. | Start with an isolated tmux pane. | Start foreground `manager start --notify none` with a failing command. | Job status reaches `failed`, the log path exists, the compact summary shows the latest failed event, and manager status remains `waiting_for_codex`. |
| `manager-run-next` | Main Codex can start follow-up work through the manager. | Start a manager and let its first job succeed. | Call `manager run-next` with a second job and command. | The second command runs in the same manager workflow, the second output file is written, and manager status returns to `waiting_for_codex` for the second job. |
| `manager-multi-pane` | One manager monitors multiple visible worker panes. | Start a manager with a slow first job. | Call `manager submit --new-worker` with a fast second job. | The fast job succeeds first while the slow job remains active, the slow job is still detected later, `worker_pane_ids` contains both panes, and the compact dashboard summarizes active/error jobs with readable pane labels without printing full paths. |
| `manager-tui-delete-completed` | The compact manager TUI can delete completed/error rows without touching active work. | Start a manager with one terminal job and one active job. | Send the viewer delete key and inspect manager state. | Terminal `succeeded`/`failed` job rows and manager-owned command/status/log evidence are removed, active jobs remain active, and no panes/windows are closed. |
| `manager-bridge-random-notify` | Bridge notification reaches Codex and is acknowledged by `manager ack`. | Start a background manager attached to a live `codex app-server --listen unix://PATH` and `codex --remote unix://PATH` target. | Submit a random-number command through `manager submit` and let the bridge turn handle the event. | Records show `last_notification.mode == bridge`, `submitted_to_app_server == true`, `acknowledged_by_codex == true`, `last_ack.event_id == last_terminal_event_id`, and the event read token was consumed. If the target Codex also writes the number response file, the scenario validates its format, but the manager workflow gate is the scoped read plus ack rather than main-turn polling. |
| `manager-tmux-inject-wakes-current-codex` | tmux-inject notification wakes an already-open Codex TUI in tmux. | Start an isolated real `codex` TUI pane, accept its temp-workspace trust prompt, bind `manager start --notify tmux-inject --codex-pane PANE_ID` to that pane, and submit a command that prints `ATTEMPT=N`, `tick 1/15` through `tick 15/15`, then `RANDOM_DIGIT=N`. | After the 15 second terminal event, the manager pastes the short wake prompt and presses the bounded composer submit key. The manager uses deterministic bound-pane capture inspection, without `OPENAI_API_KEY`, to decide whether the prompt remains staged and whether a follow-up submit/queue key is needed; with the configured Codex sidecar, it also records bounded terminal-event and receipt-recovery assessments. If no ack arrives, the manager keeps the event in `queued_in_codex` or `awaiting_receipt`, rechecks receipt every configured interval, and retries the same wake prompt only when no same/different wake id is visible and the Codex sidecar explicitly chooses `retry`; `wait`, `block`, sidecar failure, working state, user text, visible same wake id, visible different wake id, or retry/check limits prevent automatic reinjection. If active user composer text blocks the first injection, the event moves to `deferred` and is not auto-injected after the composer returns to placeholder-only; `manager notification retry` or the TUI must explicitly resume it. The Codex TUI inspects manager state/output once, runs `manager ack`, and writes a Korean response file. | The manager records one injected, queued, awaiting receipt, deferred, blocked, or pending tmux notification for the terminal event, `wake_id` is present and appears as the prompt first line, `submitted_event_ids` includes the event only after a tmux submission attempt, `last_ack.event_id == last_terminal_event_id`, `events[event_id].acknowledged_by_codex` matches the notification ack state, the prompt hash matches the short wake-only prompt, and the Codex response matches `숫자는 N이 나왔습니다.` for the printed digit. |
| `manager-random-repeat-until-zero-one` | The random notify demo repeats until the result is `0` or `1`, excluding attempt 1. | Use the verified bridge manager from the random notify path. | Submit the first random-number job through `manager submit`, then queue subsequent attempts through `manager run-next` after each bridge ack until an eligible result appears, always running at least a second attempt when attempt 1 is already `0` or `1`. | Each terminal event is bridge-acknowledged, the final reported number is `0` or `1`, and the manager remains alive after jobs complete. |
| `manager-start-reuses-live-process` | Repeated `manager start` does not create a second live manager or extra panes. | Start a manager and let its first job reach `waiting_for_codex`. | Call `manager start` again with the same manager id while the first manager process is still alive. | The second job is queued to the existing manager, the manager PID stays the same, and manager/worker pane ids and pane count stay unchanged. |
| `manager-cancel` | Manager cancellation preserves panes unless worker stop is requested. | Start a manager with a long-running worker command. | Call `manager cancel` once without `--stop-worker`, then call it again with `--stop-worker`. | The first cancel stops the manager/viewer lifecycle without stopping the worker or closing panes; the second cancel sends an interrupt and the worker job records `stopped`. |
| `manager-process-exit-keeps-worker` | Worker job survives Codex-owned manager exit. | Start a foreground manager with a delayed worker command. | Terminate the manager process directly to simulate Codex exit. | The manager process exits, the worker job keeps running in tmux, then reaches `succeeded` and writes the expected output. |

`manager-tmux-inject-wakes-current-codex`의 Korean response file은 diagnostic artifact다. 파일이 있으면 format을 검증하지만, success gate는 tmux submission attempt, event-scoped read token consumption, `manager ack`, notification/event ack state, and prompt hash match다.

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

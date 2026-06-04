# tmux-control bridge 구현 계획

## Goal

`tmux-control bridge`는 `.codex/tmux-skills`에 기록된 tmux job event와 ready task를 관찰하고, 같은 Codex app-server의 지정된 main Codex thread에 event notification prompt만 제출해 idle main Codex CLI를 깨운다.

성공 기준:

- PoC에서 `codex app-server --listen unix://`와 `codex --remote unix://`로 연결된 main CLI가, bridge가 같은 app-server/thread에 제출한 prompt로 깨어나는지 확인한다.
- 이 PoC는 hard gate다. 최소 app-server client가 실제 wake를 증명하고 그 protocol fixture를 테스트에 고정하기 전에는 bridge daemon, background start, cancel wiring을 구현하지 않는다.
- bridge는 status/log를 요약하지 않고, 실패 원인을 판단하지 않고, 코드를 수정하지 않고, model을 호출하지 않고, tmux pane에 `send-keys` 하지 않는다.
- bridge가 제출하는 prompt는 workspace, job/status/task/log path 같은 evidence pointer만 담고, 이후 분석/수정/재실행/보고는 깨어난 main Codex가 `$tmux-control`로 수행한다.
- `python scripts/tmux_control.py bridge register|start|status|cancel`이 동작하고, bridge state는 workspace-scoped `.codex/tmux-skills` 아래에 보존된다.

근거 문서:

- OpenAI Codex App Server 문서: `codex app-server`는 JSON-RPC 2.0 messages를 지원한다. `stdio://`는 JSONL이고, `unix://`/`unix://PATH`는 HTTP Upgrade handshake를 쓰는 WebSocket-over-Unix transport이며 WebSocket text frame 하나가 JSON-RPC message 하나다. `thread/resume`과 `turn/start`로 thread/session에 user turn을 시작할 수 있다. https://developers.openai.com/codex/app-server
- OpenAI Codex SDK 문서: Python SDK는 local Codex app-server over JSON-RPC를 제어한다. 다만 이 repo의 helper가 현재 stdlib 중심이라 v1에서는 SDK dependency 대신 stdlib Unix socket WebSocket client를 사용한다. https://developers.openai.com/codex/sdk#python-library
- OpenAI Codex Hooks 문서: `Stop` hook의 `decision: "block"`은 active turn continuation prompt를 만들 수 있지만 dormant thread 자체를 깨우는 수단은 아니다. https://developers.openai.com/codex/hooks
- repo 문서: `docs/workflows-and-features.md`, `docs/managed-workers.md`, `references/HOOKS.md`는 status/tasks/jobs 계약과 “hooks do not wake dormant thread” 제약을 이미 설명한다.

## Assumptions

- 사용자는 PoC와 실제 등록 시 main Codex thread id를 명시한다. bridge는 “가장 그럴듯한 thread”를 추론하지 않는다.
- bridge v1은 app-server endpoint를 `unix://PATH`로 제한한다. `unix://` default socket은 Codex default control socket path가 안정적으로 discoverable하다고 증명될 때까지 blocked input으로 다룬다. `ws://`/`wss://`는 app-server auth와 외부 노출 리스크가 있어 별도 후속 범위로 둔다.
- bridge v1의 app-server 연결은 새 dependency 없이 Python stdlib `socket.AF_UNIX`로 Unix socket에 접속하고, RFC 6455 HTTP Upgrade handshake와 masked WebSocket text frames를 직접 구현한다. `codex app-server proxy`는 live PoC에서 Unix socket WebSocket protocol로 JSONL을 변환하지 않아 실패했으므로 사용하지 않는다.
- bridge는 OpenAI API를 직접 호출하지 않으므로 별도 `OPENAI_API_KEY`를 요구하지 않는다. 같은 local `codex app-server`와 `codex --remote unix://`가 사용하는 기존 Codex auth/session, 즉 `codex login`으로 저장된 ChatGPT/Codex 인증 컨텍스트를 사용한다.
- `turn/start`는 target thread가 idle일 때만 wake prompt를 넣는다. active turn 충돌이나 app-server error는 bridge state에 delivery failure로 기록하고, bridge가 `turn/steer`, `thread/shellCommand`, `codex exec resume`, standalone Codex process, tmux `send-keys` fallback을 사용하지 않는다.
- bridge는 existing hook ack 파일인 `.codex/tmux-skills/acks`를 쓰지 않는다. hook behavior를 바꾸지 않기 위해 bridge 전용 `observed_event_ids`로 dedupe한다.
- terminal status에 `event_id`가 없으면 `tmux_state.normalize_status()`가 생성한 deterministic `event_id`를 사용한다. 그래도 event id가 없거나 malformed면 bridge는 wake하지 않고 `last_error`에 기록한다.
- ready task event id는 `ready-task:<task_id>:<matched_status.event_id>`를 우선 사용한다. matched terminal status가 없으면 `ready-task:<task_id>:<task_path>:<updated_at>`를 사용한다.
- plan 작성 시점의 repo에는 project-local `AGENTS.md` 파일이 없고, 사용자 제공 global/project instructions가 적용된다.

명시적 non-goals:

- bridge가 failure triage, log summarization, retry policy, code edits, queue execution, autopilot repair를 수행하지 않는다.
- bridge가 target tmux job pane 또는 main Codex CLI pane에 키 입력을 보내지 않는다.
- bridge가 thread discovery, thread selection, multi-workspace routing을 자동 판단하지 않는다.
- bridge가 Docker 또는 Docker 기반 MCP 설치 방식을 요구하지 않는다.

## Plan

1. 문서부터 갱신한다.

   `docs/workflows-and-features.md`에 새 “Bridge wakeup” workflow를 추가한다. 내용은 bridge의 역할, non-goals, PoC-first requirement, wake prompt shape, state paths, and `bridge register|start|status|cancel` 사용 예시를 포함한다.

   `docs/managed-workers.md`에는 bridge가 managed watch/queue worker와 다른 점을 짧게 추가한다. bridge는 `.codex/tmux-skills`를 관찰하지만 queued command를 submit하지 않고, tmux pane lifecycle cleanup도 하지 않는다.

   `references/HOOKS.md`에는 기존 hooks가 dormant thread를 깨우지 못한다는 설명을 유지하면서, bridge가 hook replacement가 아니라 app-server wake companion임을 추가한다.

   `README.md`, `SKILL.md`, `llms.txt`에는 bridge command discovery와 핵심 safety rule을 짧게 추가한다.

   검증: `python3 -m unittest tests/test_docs_index.py`와 `git diff --check`.

2. PoC hard gate를 먼저 통과시킨다.

   새 파일 `scripts/codex_app_server_client.py`를 만든다. 책임은 `unix://PATH` endpoint에 stdlib Unix socket WebSocket transport로 연결하고, JSON-RPC message를 WebSocket text frame으로 보내 `initialize`, `initialized`, optional `thread/resume`, `turn/start`, notification drain을 수행하는 최소 client다.

   새 파일 `scripts/tmux_bridge.py`에 `poc` action만 먼저 만든다. 입력은 `--thread-id`, `--workspace`, `--endpoint unix://PATH`, `--prompt TEXT`다. `daemon`, `register`, `start`, `status`, `cancel`은 이 PoC가 통과한 뒤 추가한다.

   PoC flow:

   1. 사용자가 별도 터미널에서 `codex app-server --listen unix://<short-socket-path>` 실행. macOS와 Ubuntu 모두에서 path length와 symlink 문제를 피하려면 workspace 아래 실제 directory 또는 짧은 runtime directory를 사용한다.
   2. 사용자가 main CLI를 `codex --remote unix://<same-socket-path> -C <workspace>`로 연결.
   3. PoC가 stdlib Unix socket WebSocket client로 같은 socket에 연결한다.
   4. PoC가 `initialize`/`initialized` 후, 가능하면 `thread/resume`으로 지정 stored thread를 확인한다. Live main CLI `/status`의 `Session` id는 persisted rollout id가 아닐 수 있으므로 `thread/resume` failure alone is not a PoC gate failure.
   5. PoC가 `turn/start`로 wake prompt를 제출한다.
   6. main CLI가 같은 thread에서 prompt를 받고 응답을 시작하는지 사용자가 관찰한다.

   PoC가 고정해야 하는 protocol contract:

   - `initialize` request에는 local `codex app-server generate-ts --experimental` output의 `InitializeParams` shape를 따른다: `clientInfo.name="tmux-control-bridge"`, non-null `clientInfo.title`, `clientInfo.version`, and `capabilities` object with `experimentalApi=false`, `requestAttestation=false`, `optOutNotificationMethods=[]`. 성공 response 뒤 `initialized` notification을 보낸다.
   - `thread/resume`은 optional preflight다. `result.thread.id`가 요청한 `thread_id`와 같으면 `resume_thread_id`에 기록한다. `no rollout found`처럼 live CLI Session id에는 expected일 수 있는 failure는 `resume_error`에 기록하고 `turn/start`를 계속 시도한다. 다른 permanent protocol/malformed error는 failure로 처리한다.
   - `turn/start` text input item은 local generated `UserInput` shape를 따른다: `{"type":"text","text":"<wake prompt>","text_elements":[]}`. Bridge v1은 image/localImage/skill/mention input item을 쓰지 않는다.
   - First-run `turn/start` response without JSON-RPC error is provisional delivery only. After PoC fixture capture, delivery success is judged only by the fixture's `canonical_success_signal`.
   - active turn, server overloaded, socket missing, WebSocket handshake failure, EOF 같은 transport/runtime error는 retryable failure로 기록한다.
   - unsupported method, malformed WebSocket frame/JSON-RPC response, mismatched `turn/start` thread/session id는 permanent failure로 기록하고 fallback을 시도하지 않는다.
   - 성공한 실제 WebSocket request/response/notification shape를 fake-client test fixture에 반영한 뒤 client API를 고정한다.

   검증: PoC 결과 JSON은 아래 runtime evidence schema와 같은 field names를 출력한다. 사용자가 Terminal B same thread에서 prompt 수신을 확인하면 manual confirmation note를 작성하고 `python scripts/tmux_bridge.py validate-poc --runtime-json <runtime-json>`로 gate artifact를 검증한다. 실패 시 error message만 출력하고 다른 wake fallback은 시도하지 않는다.

   PoC gate artifact:

   - runtime evidence: `.codex/tmux-skills/bridge/poc-<YYYYMMDD-HHMMSS>.json`
   - protocol fixture: `tests/fixtures/app_server_unix_ws/poc-<YYYYMMDD-HHMMSS>.json`
   - manual confirmation note: `.codex/tmux-skills/bridge/poc-<YYYYMMDD-HHMMSS>.manual.md`

   Runtime evidence required fields:

   - `endpoint`
   - `supplied_thread_id`
   - `resume_thread_id`: string when `thread/resume` succeeds, otherwise `null`
   - `resume_error`: string or `null`
   - `turn_start_thread_id`
   - `delivered`
   - `response_id`
   - `turn_id`: string or `null` when the canonical success signal does not expose a turn id
   - `request_sequence`
   - `protocol_fixture_path`
   - `manual_confirmation_note_path`
   - `created_at`

   Protocol fixture required fields:

   - `requests`: exact outbound JSON-RPC objects after scrubbing only volatile request ids and timestamps when needed
   - `responses`: exact inbound response objects
   - `notifications`: exact relevant inbound notifications
   - `canonical_success_signal`: one of `turn_started_notification`, `turn_start_response`, or `both`
   - `protocol_evidence`: object recording local generated protocol source, including `command`, `observed_at`, `initialize_params`, and `turn_start_text_input`

   Manual confirmation note required fields:

   - `main_cli_thread_id`
   - `received_prompt_timestamp`
   - `received_prompt_first_line`
   - `operator_confirmation`: must be exactly `confirmed_same_thread`

   The gate passes only when `delivered=true`, `supplied_thread_id == turn_start_thread_id == main_cli_thread_id`, the fixture exists, and `operator_confirmation == "confirmed_same_thread"`. If `resume_thread_id` is present it must equal `supplied_thread_id`; if it is `null`, `resume_error` must be present. The fake app-server tests use `canonical_success_signal` as the only success signal; response id alone is accepted only if the fixture records `canonical_success_signal="turn_start_response"`.

   First-run success handling:

   - Before a fixture exists, PoC records `provisional_delivery=true` when `turn/start` returned without JSON-RPC error and the turn response or notification ties back to the supplied live thread/session id.
   - The PoC gate does not pass on provisional delivery alone. `validate-poc` promotes the artifact to `delivered=true` only when the fixture's `canonical_success_signal` is present and the manual same-thread note validates.
   - Runtime daemon delivery does not require manual confirmation. In daemon state, `delivered_observed` means app-server delivery success according to the captured canonical success signal, not proof that a human saw the TUI wake.

3. bridge state model을 추가한다.

   새 파일 `scripts/tmux_bridge.py`를 만들고, state root는 기존 `tmux_state.state_paths()`를 사용한다.

   새 directory:

   - `.codex/tmux-skills/bridge/`

   bridge record path:

   - `.codex/tmux-skills/bridge/<bridge_id>.json`

   bridge record fields:

   - `version`
   - `bridge_id`
   - `status`: `registered|starting|active|failed|cancelled`
   - `workspace`
   - `state_dir`
   - `thread_id`
   - `endpoint`
   - `socket_path`: `null` for default `unix://`, explicit path for `unix://PATH`
   - `pid`
   - `created_at`
   - `updated_at`
   - `heartbeat_at`
   - `last_wake_at`
   - `last_error`
   - `poll_interval_seconds`
   - `quiet_seconds`
   - `observed_event_ids`: bounded list, default last 500
   - `last_delivery`: object with `event_id`, `delivered_at`, `prompt_sha256`, and app-server response metadata
   - `pending_delivery`: object with `event_id`, `attempted_at`, `prompt_sha256`, `failure_class`, and error text for the latest failed attempt
   - `observed_event_cutoff`: sort key of the oldest evicted delivered event, or `null`

   bridge lock path:

   - `.codex/tmux-skills/bridge/<bridge_id>.lock`

   `tmux_state.state_paths()`에 `bridge` key를 추가하고 `ensure_state_dirs()`가 bridge directory도 만들게 한다.

   record write 규칙:

   - 모든 bridge record write는 기존 `tmux_state.atomic_write_json()`을 사용한다.
   - every bridge record read-modify-write uses `<bridge_id>.lock`: daemon, parent background starter, register, start, and cancel all lock, reread, validate current status, merge, then atomic-write.
   - lock implementation uses an existing repo lock helper if one already exists at implementation time; otherwise use Python `fcntl.flock` exclusive locks on Unix. Do not introduce a cross-platform lock dependency for v1.
   - daemon은 lock 안에서 update 직전에 record를 다시 읽고, `status=cancelled`이면 write 없이 즉시 종료한다.
   - daemon heartbeat/update may update only `status` (`active` or `failed`), `pid`, `heartbeat_at`, `updated_at`, `last_error`, `pending_delivery`, `last_delivery`, `last_wake_at`, `observed_event_ids`, and `observed_event_cutoff`.
   - daemon heartbeat/update must preserve `thread_id`, `endpoint`, `socket_path`, `workspace`, `state_dir`, `poll_interval_seconds`, and `quiet_seconds`.
   - `cancelled` status has priority over every daemon heartbeat/update. A daemon that reloads `cancelled` exits without changing it back to `active`.
   - `cancel`은 `status=cancelled`를 우선 기록한다. 그 뒤 daemon heartbeat가 race로 들어와도 다음 daemon loop가 cancelled를 읽고 종료해야 한다.
   - unreadable/partial record는 bridge command가 `failed` JSON을 출력하고 새 record로 덮어쓰지 않는다.
   - retryable delivery failure updates `pending_delivery` and `last_error` only. It must not update `observed_event_ids`, `last_delivery`, or `last_wake_at`.
   - permanent delivery failure sets `status=failed`, clears `pending_delivery`, records `last_error`, and leaves the triggering event unobserved.
   - unreadable state files under `status/`, `tasks/`, or `jobs/` use `last_error` only; there is no separate `state_errors` field in v1.

   single-instance 규칙:

   - `start`는 lock을 잡은 상태에서 existing record의 live pid와 command-line match를 확인한다.
   - 같은 `bridge_id`의 matching daemon이 live이면 duplicate start를 거부하고 existing pid를 출력한다.
   - pid가 없거나 죽었거나 command line이 mismatch면 stale로 보고 새 daemon start를 허용하되, stale evidence를 record에 남긴다.
   - normal `start` accepts only `status=registered`.
   - `start --replace` accepts `status=failed`, `status=cancelled`, or derived stale/dead/mismatch states when no live matching daemon exists.

   observed id pruning:

   - keep the newest 500 delivered `observed_event_ids`.
   - every candidate has `event_sort_key = (event_timestamp, source_rank, event_id)`.
   - `event_timestamp` for terminal status is normalized `ended_at`, then `updated_at`; if both are missing/invalid, skip the candidate and record `last_error`.
   - `event_timestamp` for ready task is matched status `ended_at`, then matched status `updated_at`, then task `updated_at`; if all are missing/invalid, skip the candidate and record `last_error`. Do not fall back to file mtime for ready tasks.
   - `source_rank` is `0` for ready task and `1` for terminal status.
   - when pruning, set `observed_event_cutoff` to the highest `event_sort_key` among evicted delivered events.
   - a candidate with sort key `<= observed_event_cutoff` is ignored even if its event id is no longer in the bounded list. This prevents old status files from being re-woken after pruning.

   검증: unit tests for directory creation, atomic bridge record write/read, reload-before-update merge, duplicate start rejection, stale pid takeover, bounded observed id pruning, cutoff-based old-event suppression.

4. event detection을 구현한다.

   `tmux_bridge.py`는 bridge daemon loop에서 다음만 관찰한다.

   - `status/*.json`: `tmux_state.load_statuses_normalized()`로 읽고 `tmux_state.is_terminal(status)`가 true이며 bridge `observed_event_ids`에 없는 status `event_id`.
   - `tasks/*.json`: `tmux_state.load_task_state()`와 `tmux_state.classify_task_state()`로 읽은 `ready_tasks`. ready task event id는 `ready-task:<task_id>:<matched_status.event_id or updated_at>`로 만든다.
   - `jobs/*.json`: v1에서는 직접 wake source가 아니라 diagnostics only다. bridge prompt에는 관련 ready task/status가 참조하는 path가 있을 때만 job path를 포함한다.

   bridge는 unreadable JSON을 판단하지 않고 `last_error`만 기록한다. unreadable file 자체로 wake하지 않는다.

   event ordering:

   - candidate priority is ready tasks first, sorted oldest by `event_sort_key`; if no ready task candidate exists, terminal statuses are sorted newest by `event_sort_key`.
   - 한 poll cycle에서 하나의 wake prompt만 보낸다.
   - `quiet_seconds` default 10으로 burst를 억제하지만 event를 버리지 않는다.

   event state machine:

   - `candidate`: event가 감지되었지만 throttle 또는 delivery 전 상태다. `observed_event_ids`에 추가하지 않는다.
   - `delivery_attempted`: app-server request를 보냈지만 성공하지 못했다. `pending_delivery`와 `last_error`만 갱신하고 `observed_event_ids`에 추가하지 않는다.
   - `delivered_observed`: `turn/start` 성공이 확인된 상태다. 이때만 `observed_event_ids`, `last_delivery`, `last_wake_at`를 갱신하고 `pending_delivery`를 비운다.
   - throttle 중 발견된 event는 `candidate`로 남는다. 나중 poll에서 ordering 규칙에 따라 delivery한다.
   - after `retryable_failure`, retry the same still-top candidate only after `quiet_seconds` has elapsed from `pending_delivery.attempted_at`. It remains unobserved. A newly appearing higher-priority ready task can be delivered first by the normal ordering rule.
   - during a quiet window, candidate selection is recomputed every poll. If a higher-priority ready task appears, it may be delivered first; the previous pending event remains unobserved and eligible after the quiet window.

   검증: unit tests for terminal status detection, ready task detection, event id fallback/malformed skip, dedupe, unreadable state handling, one-wake-per-cycle ordering, `first poll throttled -> event remains unobserved -> later poll delivered`, and `ready task before terminal status`.

5. wake prompt builder를 구현한다.

   prompt는 고정 template만 사용한다. status/log contents나 `last_output`은 넣지 않는다.

   ```text
   tmux-control observed a terminal event.

   Workspace: <workspace>
   Job ID: <job_id or unknown>
   Status path: <status_path or none>
   Task path: <task_path or none>
   Log path: <log_path or none>

   Please use $tmux-control to inspect the status and logs, then continue the requested work.
   ```

   ready task일 때 첫 줄은 `tmux-control observed a ready task.`로만 바꾸고, task instruction 전문은 넣지 않는다. task instruction은 main Codex가 `task load`/`task next`로 읽는다.

   검증: prompt snapshot tests ensure no `last_output`, no log tail, no failure analysis text, no command output.
   snapshot은 terminal event와 ready task case를 따로 둔다. 금지 문자열/source는 `last_output`, `stdout`, `stderr`, `tail`, `traceback`, `Traceback`, `instruction`, task instruction body, suggested commands, model/delegate language, status/log content excerpt다.

6. app-server delivery를 구현한다.

   `codex_app_server_client.py` API:

   - `AppServerClient(endpoint: str, codex_bin: str = "codex")`
   - `connect()`
   - `initialize(client_name="tmux-control-bridge")`
   - `resume_thread(thread_id: str)`
   - `start_turn(thread_id: str, prompt: str, cwd: str | None)`
   - `close()`
   - `close()` sends a WebSocket close frame when possible, closes the Unix socket, and is safe to call from a `finally` path after initialize/resume/start exceptions.

   For `unix://PATH`, open `PATH` with `socket.AF_UNIX`, perform HTTP WebSocket Upgrade, validate `101 Switching Protocols` and `Sec-WebSocket-Accept`, then send one JSON-RPC message per masked WebSocket text frame.

   For `unix://`, return a permanent validation error in v1 unless a later documented default socket discovery path is added and tested on both macOS and Ubuntu.

   `turn/start` input:

   ```json
   {
     "method": "turn/start",
     "params": {
       "threadId": "<thread_id>",
       "input": [{"type": "text", "text": "<wake prompt>", "text_elements": []}],
       "cwd": "<workspace>"
     }
   }
   ```

   `initialize` input:

   ```json
   {
     "method": "initialize",
     "params": {
       "clientInfo": {
         "name": "tmux-control-bridge",
         "title": "tmux-control bridge",
         "version": "0.1"
       },
       "capabilities": {
         "experimentalApi": false,
         "requestAttestation": false,
         "optOutNotificationMethods": []
       }
     }
   }
   ```

   If `turn/start` fails, bridge records `last_error`, does not mark the event observed, and retries after normal polling/throttle. `thread/resume` is optional evidence only and a live-session `no rollout found` does not block `turn/start`. If `turn/start` succeeds according to the captured canonical success signal, bridge appends event id to `observed_event_ids` and updates `last_wake_at`.

   delivery result classes:

   - `delivered`: turn start succeeded for the supplied thread/session id according to the captured canonical success signal; `thread/resume` may have succeeded or may have produced a recorded `resume_error`.
   - `retryable_failure`: active turn conflict, temporary app-server/socket/WebSocket failure, server overloaded, timeout, EOF after request without permanent error.
   - `permanent_failure`: unsupported method, invalid endpoint syntax, malformed WebSocket/JSON-RPC response, mismatched turn thread/session id.

   For `retryable_failure`, event remains unobserved. For `permanent_failure`, daemon records `status=failed` and does not retry until `bridge register` or `bridge start --replace` refreshes state.

   검증: fake Unix socket WebSocket app-server tests for handshake, initialize/resume/turn request order, delivered/retryable/permanent classification, EOF handling, response id correlation, and no fallback command execution.
   Fake app-server tests must enforce allowed outbound request methods: `initialize`, `thread/resume`, and `turn/start` only. They must separately allow the client notification `initialized`. Any `turn/steer`, `thread/shellCommand`, `mcpServer/tool/call`, `model/list`, `thread/list`, `thread/loaded/list`, or other outbound app-server request method fails the test. `turn/start` is allowed only when its text exactly matches the wake prompt builder output.
   Tests must also assert that the outbound `initialize` request includes `clientInfo.title`, `clientInfo.version`, and `capabilities`, and that the outbound `turn/start` text item includes `text_elements: []`, matching local `codex app-server generate-ts --experimental` protocol evidence captured before the PoC gate.

7. CLI를 `tmux_control.py`에 연결한다.

   Add subparser:

   ```bash
   python scripts/tmux_control.py bridge register --thread-id THREAD --endpoint unix://PATH [--bridge-id ID] [--poll-seconds N] [--quiet-seconds N] [--replace] [--workspace PATH] [--state-dir PATH]
   python scripts/tmux_control.py bridge start --bridge-id ID [--foreground|--background] [--replace] [--workspace PATH] [--state-dir PATH]
   python scripts/tmux_control.py bridge status --bridge-id ID [--json] [--workspace PATH] [--state-dir PATH]
   python scripts/tmux_control.py bridge cancel --bridge-id ID [--workspace PATH] [--state-dir PATH]
   ```

   CLI contract:

   - `register`: required `--thread-id`, `--endpoint`; optional `--bridge-id` default `bridge-<safe_id(thread_id)>`; optional `--poll-seconds` default `2.0`; optional `--quiet-seconds` default `10.0`; optional `--replace`; optional `--workspace`; optional `--state-dir`.
   - `start`: required `--bridge-id`; `--foreground` and `--background` are mutually exclusive, default `--foreground`; optional `--replace`; optional `--workspace`; optional `--state-dir`. `start` reads endpoint/thread/poll settings only from the registered record and accepts no overrides for them.
   - `status`: required `--bridge-id`; optional `--json`; optional `--workspace`; optional `--state-dir`. It is read-only except for reporting derived stale/mismatch fields in output.
   - `cancel`: required `--bridge-id`; optional `--workspace`; optional `--state-dir`.
   - `poc`: required `--thread-id`, `--endpoint`, `--workspace`, `--prompt`; optional `--state-dir`.
   - Endpoint validation accepts only `unix://<nonblank path>` in v1. Exact `unix://`, `ws://`, `wss://`, `http://`, `https://`, bare host/port, blank values, and malformed URLs fail before any app-server connection or wake attempt.
   - Thread id validation requires nonblank text. Missing or blank thread id fails before state write for `poc`; for `register`, it exits without writing a record. A wrong but syntactically nonblank thread id is detected by `thread/resume` and recorded as permanent failure during `poc` or daemon delivery.

   `--replace` meaning:

   - `--replace` never replaces a live matching daemon.
   - `register --replace` may overwrite an existing `failed` or `cancelled` record when no live matching daemon exists; it resets `status=registered`, clears `last_error` and `pending_delivery`, and preserves no prior `observed_event_ids`.
   - Dropping prior `observed_event_ids` on `register --replace` is intentional reset semantics; duplicate wake of old still-present files is acceptable only after an explicit replace.
   - `start --replace` may start from `failed`, `cancelled`, stale, dead, or command-line-mismatched records when no live matching daemon exists. It does not change `thread_id` or `endpoint`; use `register --replace` first for config changes.

   Internal entrypoints:

   ```bash
   python scripts/tmux_bridge.py poc --thread-id THREAD --endpoint unix://PATH --workspace PATH --prompt TEXT
   python scripts/tmux_bridge.py validate-poc --runtime-json PATH
   python scripts/tmux_bridge.py daemon --bridge-id ID --workspace PATH [--state-dir PATH]
   ```

   Behavior:

   - `register`: validates nonblank thread id and endpoint syntax, writes `status=registered`. It does not send a prompt.
   - `start --foreground`: calls the daemon function in the current process, suitable for a small tmux pane.
   - `start --background`: launches `[sys.executable, scripts/tmux_bridge.py, "daemon", "--bridge-id", ID, "--workspace", WORKSPACE, ...]` with stdout/stderr redirected to `.codex/tmux-skills/bridge/<bridge_id>.daemon.log`. Parent records `pid` immediately with `status=starting` under lock, releases the lock, then waits up to 5 seconds for the child to write `status=active` and `heartbeat_at`. If the child exits early or no heartbeat appears before timeout, parent reacquires the lock, marks `status=failed`, records `last_error`, and returns `started=false`.
   - `status`: reads bridge record, checks whether stored pid is live and command line matches `tmux_bridge.py daemon --bridge-id <id>`, and prints JSON/text.
   - `cancel`: first marks record `cancelled`, then signals only a live pid whose command line contains `tmux_bridge.py daemon --bridge-id <id>` and whose record workspace matches the command workspace. If pid is live but command line mismatches, it sends no signal and records stale/mismatch evidence. It never closes panes/windows.

   검증: parser tests and process-match tests modeled after existing `job status/cancel` safety behavior. Include pid reuse cases where the pid is live but command line mismatches; signal must not be sent.

8. Integration and E2E tests를 추가한다.

   Unit tests:

   - `tests/test_tmux_bridge.py`: state model, detection, prompt builder, dedupe, throttle, cancel safety.
   - `tests/test_codex_app_server_client.py`: fake Unix socket WebSocket protocol, JSON-RPC request ordering, error propagation.
   - `tests/test_tmux_control.py`: bridge subcommand parsing and dispatch.
   - `tests/test_tmux_bridge_poc_artifacts.py`: validates runtime JSON, fixture JSON, manual note fields, thread id equality, `delivered=true`, fixture existence, canonical success signal, and prompt first line.
   - docs index tests updated if new docs sections are linked.
   - subprocess spy tests: delivery must not spawn any process. Any `codex app-server proxy`, `codex exec`, `codex exec resume`, standalone `codex`, `tmux`, or `send-keys` invocation fails the test.
   - CLI validation tests: missing/blank `thread_id`, unsupported endpoints (`ws://`, `wss://`, `http://`, bare host/port), malformed bridge record, and wrong thread id behavior. Unsupported endpoints and missing thread ids must fail before any wake attempt.

   Optional local manual PoC:

   - Requires real `codex` and interactive main CLI.
   - Not part of normal unittest because it needs a live app-server and human observation of the TUI.
   - Document exact commands and expected observations in `docs/workflows-and-features.md`.
   - Save the PoC command, bridge JSON output, protocol fixture path, and same-thread manual confirmation note in docs or the plan Review Status before proceeding past the hard gate.

   Full verification:

   ```bash
   PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover
   PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile scripts/*.py
   git diff --check
   python3 scripts/tmux_control.py --help
   python3 scripts/tmux_control.py bridge --help
   ```

9. Rollout order and rollback.

   Implementation order:

   1. docs updates
   2. `codex_app_server_client.py` minimal Unix WebSocket client plus `tmux_bridge.py poc`
   3. manual PoC hard gate and protocol fixture capture
   4. fake-client tests updated from the captured protocol fixture
   5. `tmux_bridge.py` state/detection/prompt/daemon tests
   6. `tmux_control.py bridge` CLI wiring/tests
   7. final docs polish based on PoC result

   Rollback is simple because bridge uses a new `.codex/tmux-skills/bridge` directory and new scripts. If PoC fails, stop before daemon/CLI start work; leave only docs noting the blocked app-server wake assumption and the failing command/error evidence.

## Test Plan

- Run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests/test_codex_app_server_client.py tests/test_tmux_bridge.py tests/test_tmux_control.py tests/test_docs_index.py`.
- Run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover`.
- Run `PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile scripts/*.py`.
- Run `git diff --check`.
- Run CLI smoke checks:
  - `python3 scripts/tmux_control.py bridge --help`
  - `python3 scripts/tmux_control.py bridge register --thread-id thr_test --endpoint unix://"$(pwd)/.codex/tmux-skills/bridge/sockets/codex-bridge.sock" --workspace "$(pwd)"`
  - `python3 scripts/tmux_control.py bridge status --bridge-id <printed-id> --workspace "$(pwd)" --json`
  - `python3 scripts/tmux_control.py bridge cancel --bridge-id <printed-id> --workspace "$(pwd)"`
- Record or clean up any CLI smoke record under `.codex/tmux-skills/bridge`; tests must assert generated smoke artifacts stay inside that directory.
- Run fake app-server delivery tests that assert no `thread/shellCommand`, no `turn/steer`, no `codex exec resume`, no tmux command invocation.
  - Allowed outbound JSON-RPC request methods are exactly `initialize`, `thread/resume`, and `turn/start`; allowed outbound notification method is `initialized`.
  - `turn/start` text must equal the wake prompt builder output.
- Run subprocess spy tests that fail if delivery starts any subprocess, including `codex app-server proxy`, `codex exec`, `resume`, `tmux`, or `send-keys`.
- Run prompt snapshot tests that fail if prompt includes `last_output`, `stdout`, `stderr`, `tail`, `traceback`, status tail, log contents, failure diagnosis, `instruction`, or task instruction body.
- Run throttle/dedupe tests:
  - first poll throttled -> event remains absent from `observed_event_ids`
  - later poll delivered -> event enters `observed_event_ids`
  - multiple candidates -> oldest ready task wakes before terminal status
  - one poll cycle -> one wake only
  - delivery success 전에는 observed id 기록 금지
  - retryable delivery failure 후 daemon restart -> same event remains candidate and can retry
  - permanent failure 후 `register --replace` or `start --replace` 없이는 restart/retry 불가
- Run daemon lifecycle tests:
  - duplicate `bridge_id` start with live matching pid is rejected
  - stale/dead pid can be replaced
  - live pid with mismatched command line is never signaled by `cancel`
  - `cancel` marks record cancelled before signal attempt
  - background child exits before heartbeat -> parent records failed and no active pid is reported
  - background child writes heartbeat -> parent reports started true and active record
- Run CLI validation tests:
  - `bridge register` missing/blank `--thread-id` exits nonzero without writing state
  - `bridge register --endpoint ws://127.0.0.1:4500` exits nonzero before app-server connection
  - malformed bridge record makes `status/start/cancel` return failed JSON without overwriting the file
- Run PoC artifact validation tests:
  - missing `protocol_fixture_path`, missing manual note, thread id mismatch, ambiguous `operator_confirmation`, or missing canonical success signal fails validation
  - `operator_confirmation` must equal `confirmed_same_thread`
  - `response_id` is required; `turn_id` may be `null` only when the fixture canonical success signal lacks a turn id
- Run manual PoC after minimal `codex_app_server_client.py`/`tmux_bridge.py poc` unit tests pass, and before daemon/CLI wiring:
  - Terminal A: `codex app-server --listen unix:///short/real/path/codex-bridge.sock`
  - Terminal B: `codex --remote unix:///short/real/path/codex-bridge.sock -C /Users/dwchoo/project/tmux-skills`
  - Terminal C or bridge pane: `python3 scripts/tmux_bridge.py poc --thread-id <main-session-id-from-status> --endpoint unix:///short/real/path/codex-bridge.sock --workspace /Users/dwchoo/project/tmux-skills --prompt "tmux-control observed a terminal event..."`
  - Pass condition: Terminal B receives the prompt in the same session/thread and starts the main Codex turn; validated runtime evidence reports `delivered=true`, matching `supplied_thread_id`, `turn_start_thread_id`, and `main_cli_thread_id`, response id, and preferably `turn_id`; this evidence is recorded before daemon/CLI wiring proceeds. `resume_thread_id` may be `null` when `thread/resume` reports no stored rollout for a live CLI Session id.
  - Gate command: `python3 scripts/tmux_bridge.py validate-poc --runtime-json .codex/tmux-skills/bridge/poc-<YYYYMMDD-HHMMSS>.json`

## Review Status

- latest_pass: 7
- reviewers: plan_critic, plan_architect, plan_verifier, live_poc
- gate_status: poc_gate_passed
- cleanup_status: All reviewer agents closed.
- non_waived_blocking_gaps: live PoC failed before daemon/CLI wiring. `codex app-server --listen unix://` plus `codex --remote unix:// -C /Users/dwchoo/project/tmux-skills` started in tmux, main CLI `/status` reported Session `019e91a7-8e2c-70a3-944a-66c4af57655e`, but `python3 scripts/tmux_bridge.py poc --thread-id 019e91a7-8e2c-70a3-944a-66c4af57655e --endpoint unix:// ...` timed out waiting for the `initialize` JSON-RPC response. App-server pane logged `failed to upgrade control socket websocket connection: WebSocket protocol error: httparse error: invalid token`, which indicates the planned `codex app-server proxy` stdio path did not translate line-delimited JSON-RPC to the Unix socket WebSocket protocol for this live setup. Runtime evidence: `.codex/tmux-skills/bridge/poc-20260604-170808.json`. Protocol fixture: `tests/fixtures/app_server_proxy/poc-20260604-170808.json`. `validate-poc` correctly rejected the artifact because outbound sequence stopped at `initialize`.
- live_ws_probe: 2026-06-04T17:17+09:00 proved Python stdlib WebSocket-over-Unix on macOS using `codex app-server --listen unix:///Users/dwchoo/project/tmux-skills/.codex/tmux-skills/bridge/sockets/ws-test.sock`; `initialize` returned app-server metadata, `turn/start` against main CLI Session `019e91b5-7317-7c91-992d-c5ba4ed8cefd` woke the `codex --remote unix://...ws-test.sock` TUI with the path-only prompt. `thread/resume` returned `no rollout found for thread id ...`, so `thread/resume` must be optional for live CLI Session ids.
- waived_overreach: none
- local_protocol_evidence: 2026-06-04 generated via `codex app-server generate-ts --experimental`; `InitializeParams` requires `clientInfo` plus `capabilities`, `ClientInfo` includes `name`, `title`, and `version`, and `v2/UserInput` text variant includes `text_elements`.
- next_options: implement minimal stdlib WebSocket-over-Unix transport for `unix://PATH`, regenerate successful PoC artifacts under `tests/fixtures/app_server_unix_ws`, then continue daemon/background/cancel wiring only after `validate-poc` passes.
- live_poc_gate: 2026-06-04T17:31+09:00 passed with explicit workspace socket `unix:///Users/dwchoo/project/tmux-skills/.codex/tmux-skills/bridge/sockets/live-poc.sock`. Main CLI `/status` Session `019e91c1-2c5e-7c92-a431-d173b8b3c5f5` received the path-only prompt through `turn/start`; `thread/resume` returned recorded `no rollout found`, expected for this live CLI Session id. Runtime artifact `.codex/tmux-skills/bridge/poc-20260604-173115.json`, fixture `tests/fixtures/app_server_unix_ws/poc-20260604-173115.json`, manual note `.codex/tmux-skills/bridge/poc-20260604-173115.manual.md`; `validate-poc` returned `valid=true`.
- next_options_after_gate: continue bridge state/detection/prompt/daemon lifecycle wiring, CLI contract validation, and review-test-fix loop.
- last_updated: 2026-06-04T17:45:00+09:00

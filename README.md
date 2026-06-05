# tmux-skills

Codex skill for controlled tmux usage. It provides concise skill instructions, Python helpers for tmux panes/windows, long-running job wrappers, managed watch/queue workers, resumable follow-up task records, visible status review flows, and reusable pane monitors.

## Requirements

- Python 3.10+
- `tmux` available on `PATH`

## Files

- `SKILL.md`: skill instructions and safety rules.
- `llms.txt`: LLM-friendly documentation index.
- `docs/`: canonical detailed contracts and runbooks.
- `references/`: copyable workflow examples.
- `scripts/tmux_control.py`: main tmux helper used by the skill.
- `scripts/tmux_job.py`: long-running command wrapper.
- `scripts/tmux_queue.py`: managed watch and queue-after worker.
- `scripts/tmux_monitor.py`: single-trigger pane monitor.
- `scripts/codex_app_server_client.py`: minimal stdlib Unix socket WebSocket client for bridge wake delivery.
- `scripts/tmux_bridge.py`: bridge PoC, artifact validation, and daemon entrypoint for path-only Codex thread wakeups.
- `scripts/run_managed_job.sh`: raw tmux fallback wrapper for helper-unavailable managed jobs.
- `.codex/tmux-skills/tasks/`: runtime follow-up task records created by anchored `task add` and `run --next-instruction`.
- `.codex/tmux-skills/jobs/`: managed background worker records created by `watch` and queue commands.
- `.codex/tmux-skills/bridge/`: bridge records, locks, daemon logs, and PoC runtime/manual artifacts.

## Documentation

- Start with [`llms.txt`](llms.txt) for LLM-friendly discovery.
- Use [`docs/README.md`](docs/README.md) as the docs wiki index.
- Use [`docs/llm-wiki-style-guide.md`](docs/llm-wiki-style-guide.md) before adding or reorganizing wiki pages.
- Use [`docs/workflows-and-features.md`](docs/workflows-and-features.md) for the desired operating flow and feature map.
- Use [`docs/managed-workers.md`](docs/managed-workers.md) for managed worker contracts, dedupe, stale GC, and send preflight behavior.
- Use [`docs/real-use-e2e.md`](docs/real-use-e2e.md) for the real tmux E2E harness and scenario coverage.

## Verify

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile scripts/*.py
bash scripts/run_managed_job.sh "$(mktemp -d)/job" bash -c 'printf "ok\n"'
PYTHONDONTWRITEBYTECODE=1 python3 scripts/e2e_real_use.py --scenario smoke --json
PYTHONDONTWRITEBYTECODE=1 python3 scripts/e2e_real_use.py --scenario all --json
git diff --check
python3 scripts/tmux_control.py --help
python3 scripts/tmux_control.py list
python3 scripts/tmux_control.py bridge --help
```

When outside tmux, `spawn` and `new-window` create or reuse a detached `codex-<workspace>` session and report an `attach_command`.

## Bridge wakeup

`tmux-control bridge` observes terminal event and ready task records under `.codex/tmux-skills`, then sends a path-only notification prompt to a user-specified main Codex thread through the same local `codex app-server`. It uses existing `codex login` auth/session through local app-server Unix socket WebSocket transport; it does not call the OpenAI API directly and does not require `OPENAI_API_KEY`.

PoC is a hard gate. Prove same-thread wake with `scripts/tmux_bridge.py poc`, save the protocol fixture and manual confirmation note, and run `scripts/tmux_bridge.py validate-poc` before relying on daemon or `tmux_control.py bridge start`.

The bridge never summarizes status/log content, reads task instruction bodies into prompts, diagnoses failures, retries commands, sends keys to tmux panes, runs `codex exec resume`, discovers threads, or calls app-server methods outside the allowed wake flow.

## Raw tmux fallback

Prefer `python scripts/tmux_control.py run` for long-running commands. If the helper is unavailable and you must launch a manual raw tmux fallback, run the installed wrapper from the skill directory so status, logs, PID, and exit code are still preserved:

```bash
tmux new-session -d -s <job_id> "cd ~/.codex/skills/tmux-control && bash scripts/run_managed_job.sh <workspace>/logs/jobs/<job_id> <command> <args...>"
```

The wrapper executes the command as argv without shell re-parsing, writes combined stdout/stderr to `stdout.log`, and does not create `stderr.log`.

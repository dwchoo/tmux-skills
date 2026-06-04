#!/usr/bin/env bash
set -uo pipefail

usage() {
  printf 'usage: %s <job_dir> <command> [args...]\n' "$(basename "$0")" >&2
}

artifact_error() {
  printf 'artifact write failure: %s\n' "$*" >&2
  exit 125
}

write_text_file() {
  local path="$1"
  local text="$2"
  printf '%s\n' "$text" > "$path" || artifact_error "$path"
}

write_command_file() {
  local path="$1"
  local arg
  shift
  : > "$path" || artifact_error "$path"
  for arg in "$@"; do
    printf '%q ' "$arg" >> "$path" || artifact_error "$path"
  done
  printf '\n' >> "$path" || artifact_error "$path"
}

if [ "$#" -lt 2 ]; then
  usage
  exit 2
fi

job_dir="$1"
shift

mkdir -p -- "$job_dir" || artifact_error "mkdir $job_dir"

command_file="$job_dir/command.sh"
stdout_log="$job_dir/stdout.log"
status_file="$job_dir/status.json"
exitcode_file="$job_dir/exitcode"
started_at_file="$job_dir/started_at"
finished_at_file="$job_dir/finished_at"
pid_file="$job_dir/pid"

started_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')" || artifact_error "started_at timestamp"
write_command_file "$command_file" "$@"
: > "$stdout_log" || artifact_error "$stdout_log"
write_text_file "$started_at_file" "$started_at"
write_text_file "$status_file" '{"state":"running"}'
: > "$pid_file" || artifact_error "$pid_file"

"$@" >> "$stdout_log" 2>&1 &
child_pid="$!"
write_text_file "$pid_file" "$child_pid"

wait "$child_pid"
child_status="$?"

finished_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')" || artifact_error "finished_at timestamp"
write_text_file "$exitcode_file" "$child_status"
write_text_file "$finished_at_file" "$finished_at"

if [ "$child_status" -eq 0 ]; then
  final_state="succeeded"
else
  final_state="failed"
fi
write_text_file "$status_file" "{\"state\":\"$final_state\",\"exitcode\":$child_status}"

exit "$child_status"

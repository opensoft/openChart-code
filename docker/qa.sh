#!/usr/bin/env bash
# One-command operations for the persistent synthetic QA stack: the compose
# project open-chart-qa of docker/qa-compose.yaml, with the bench on fixed host
# port 8001. See docker/QA.md; `docker/qa.sh help` lists the subcommands.
#
# Every path comes from this script's own location, so it behaves the same from
# any working directory, from an openChart assembly root (code/docker/qa.sh)
# and from a standalone openChart-code clone.

set -euo pipefail

# With CDPATH set, `cd` searches it before the working directory and may print
# the directory it chose, which would break every path below.
unset CDPATH

# Physical directory of this script. Symlinks are followed on purpose: compose
# resolves the `..` bind mount of the app from the compose file's path as
# written, so a `docker -> code/docker` link would make it mount the assembly
# root instead of the code leg (docker/QA.md, "Bring up").
resolve_script_dir() {
  local src=${BASH_SOURCE[0]} dir target
  while [ -L "$src" ]; do
    dir=$(cd -P -- "$(dirname -- "$src")" && pwd -P)
    target=$(readlink -- "$src")
    case $target in
      /*) src=$target ;;
      *) src=$dir/$target ;;
    esac
  done
  cd -P -- "$(dirname -- "$src")" && pwd -P
}

SCRIPT_DIR=$(resolve_script_dir)
CODE_ROOT=$(cd -P -- "$SCRIPT_DIR/.." && pwd -P)
COMPOSE_FILE=$SCRIPT_DIR/qa-compose.yaml
CREDENTIALS_PATH=/home/frappe/qa-sites/qa-credentials.env

# The project name is the `name:` of qa-compose.yaml. It is passed as -p to every
# compose call: -p outranks COMPOSE_PROJECT_NAME, a docker/.env file and
# COMPOSE_ENV_FILES, any of which would otherwise rename the project and point
# `down` at another stack.
QA_PROJECT=open-chart-qa
# The bench's fixed host port (docker/qa-compose.yaml).
QA_BASE_URL=http://localhost:8001

usage() {
  cat <<'EOF'
Usage: qa.sh <command>

Operate the persistent synthetic QA stack (compose project open-chart-qa,
bench on http://localhost:8001). Works from any directory.

Commands:
  up      Start the stack (docker compose up -d), then wait for the bench.
  wait    Poll until the bench container is healthy. Fails at once if the
          bench exits, is paused, restarts, turns unhealthy, stays "created"
          or logs "OC-QA-RESULT: FAIL".
  test    Run the live runner (uv run scripts/run_p4_live.py) against a
          healthy bench, with the stack's QA_API_KEY, QA_API_SECRET and
          QA_SITE. It always targets http://localhost:8001 and ignores any
          OC_API_KEY, OC_API_SECRET, OC_SITE and OC_BASE. Exits with the
          runner's exit code.
  down    Stop and remove the containers. Keeps the volumes and credentials.
  status  Show the stack's containers (docker compose ps).
  help    Show this text.

up, down and test refuse a stack that was created from another checkout.
Destroying the data is not offered here: use qa-teardown.sh beside this script.

Environment:
  QA_UP_TIMEOUT        Seconds `wait` and `up` allow (default 1200).
  QA_POLL_INTERVAL     Seconds between polls (default 5).
  QA_CREATED_GRACE     Seconds the bench may stay "created" (default 30).
  QA_ALLOW_FOREIGN_STACK=1
                       Let up, down and test operate on a stack that was
                       created from another checkout.
  OC_CONTRACTS_DIR     Spec leg contracts/ directory the stack mounts (needed
                       by `up` in a standalone clone). `test` uses it as
                       CONTRACTS_DIR when that is unset.
  CONTRACTS_DIR        Spec leg contracts/ directory the live runner reads.
  QA_ADMIN_PASSWORD, QA_DB_ROOT_PASSWORD
                       Read by compose on every `up`; a changed value
                       recreates the containers. Only the site's
                       Administrator password is fixed when the site is
                       first created.
EOF
}

die() {
  printf 'qa.sh: %s\n' "$*" >&2
  exit 1
}

usage_error() {
  printf 'qa.sh: %s\n\n' "$*" >&2
  usage >&2
  exit 2
}

compose() {
  docker compose -p "$QA_PROJECT" -f "$COMPOSE_FILE" "$@"
}

require_docker() {
  command -v docker >/dev/null 2>&1 || die "docker is not on PATH"
}

# Refuse to up, down or test a stack that was created from another checkout:
# the stack runs a copy of that checkout's app, and `down` would remove its
# containers. Compose labels every container with the directory of the compose
# file it was created from, the same directory this script's file sits in.
require_own_stack() {
  local ids id label have=''
  [ "${QA_ALLOW_FOREIGN_STACK:-}" != 1 ] || return 0
  ids=$(compose ps -a -q) || die "cannot query docker; is the daemon running?"
  id=${ids%%$'\n'*}
  [ -n "$id" ] || return 0
  label=$(docker inspect --format \
    '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' \
    "$id") || die "cannot query docker; is the daemon running?"
  if [ -n "$label" ]; then
    have=$(cd -P -- "$label" 2>/dev/null && pwd -P) || have=''
  fi
  [ "$have" = "$SCRIPT_DIR" ] && return 0
  die "the $QA_PROJECT stack was created from '${label:-an unknown directory}', not from this checkout's '$SCRIPT_DIR'. Run that checkout's qa.sh, or set QA_ALLOW_FOREIGN_STACK=1 to operate on it from here anyway."
}

# Sets BENCH_ID, BENCH_STATE, BENCH_HEALTH, BENCH_RESTARTS and BENCH_STARTED
# for the bench container. Returns 1 when there is no bench container and 2
# when docker cannot be queried. Called from `||` contexts, where set -e is
# off, so every failure is returned explicitly.
inspect_bench() {
  local ids info
  BENCH_ID=''
  ids=$(compose ps -a -q bench) || return 2
  BENCH_ID=${ids%%$'\n'*}
  [ -n "$BENCH_ID" ] || return 1
  info=$(docker inspect --format \
    '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} {{.RestartCount}} {{.State.StartedAt}}' \
    "$BENCH_ID") || return 2
  read -r BENCH_STATE BENCH_HEALTH BENCH_RESTARTS BENCH_STARTED <<<"$info" \
    || return 2
}

# Locate the bench container or stop with a message naming the way forward.
find_bench() {
  local rc=0
  inspect_bench || rc=$?
  case $rc in
    0) ;;
    1) die "no bench container for $COMPOSE_FILE; run 'qa.sh up'" ;;
    *) die "cannot query docker; is the daemon running?" ;;
  esac
}

# What a failed wait leaves on stderr: the bench's last build steps and any
# OC-QA-RESULT line, then the contracts check's result (it runs before the
# bench and, when it fails, the bench never starts). A third argument limits
# the logs to the runs since that time, so a result from before the wait does
# not appear beside the one that failed it.
show_results() {
  local label=$1 id=$2 since=${3:-} logs steps results
  [ -n "$id" ] || return 0
  if [ -n "$since" ]; then
    logs=$(docker logs --since "$since" "$id" 2>&1) || logs=''
  else
    logs=$(docker logs "$id" 2>&1) || logs=''
  fi
  steps=$(printf '%s\n' "$logs" | grep -F '== OC-QA-STEP' | tail -n 5) || true
  results=$(printf '%s\n' "$logs" | grep -F 'OC-QA-RESULT' | tail -n 5) || true
  if [ -n "$steps" ]; then
    printf 'qa.sh: last %s steps:\n%s\n' "$label" "$steps" >&2
  fi
  if [ -n "$results" ]; then
    printf 'qa.sh: %s result:\n%s\n' "$label" "$results" >&2
  fi
}

show_diagnostics() {
  local check_id=''
  show_results bench "$BENCH_ID" "$RUN_SINCE"
  check_id=$(compose ps -a -q contracts-check 2>/dev/null | head -n 1) || true
  show_results contracts-check "$check_id"
}

wait_failed() {
  printf 'qa.sh: %s\n' "$1" >&2
  show_diagnostics
}

latest_step() {
  docker logs --since "$BENCH_STARTED" "$BENCH_ID" 2>&1 \
    | grep -F '== OC-QA-STEP' | tail -n 1 || true
}

# Whether the current run of the bench logged a failure. The failure line is
# the last thing the init script writes before the container exits.
bench_logged_failure() {
  local tail_logs
  tail_logs=$(docker logs --since "$BENCH_STARTED" --tail 200 "$BENCH_ID" 2>&1) \
    || return 1
  case $tail_logs in
    *'OC-QA-RESULT: FAIL'*) return 0 ;;
    *) return 1 ;;
  esac
}

cmd_wait() {
  local timeout=${QA_UP_TIMEOUT:-1200} interval=${QA_POLL_INTERVAL:-5}
  local grace=${QA_CREATED_GRACE:-30}
  local start=$SECONDS elapsed next_note=0 base_restarts='' created_since='' step
  case $timeout in
    '' | *[!0-9]*) die "QA_UP_TIMEOUT must be whole seconds, got '$timeout'" ;;
  esac
  case $interval in
    '' | 0 | *[!0-9]*) die "QA_POLL_INTERVAL must be whole seconds above 0" ;;
  esac
  case $grace in
    '' | *[!0-9]*) die "QA_CREATED_GRACE must be whole seconds, got '$grace'" ;;
  esac
  require_docker
  RUN_SINCE=''
  while :; do
    find_bench
    if [ -z "$base_restarts" ]; then
      base_restarts=$BENCH_RESTARTS
      RUN_SINCE=$BENCH_STARTED
    fi
    case $BENCH_STATE in
      exited | dead | removing | restarting | paused)
        wait_failed "the bench container is $BENCH_STATE"
        return 1
        ;;
      created)
        [ -n "$created_since" ] || created_since=$SECONDS
        if [ $((SECONDS - created_since)) -ge "$grace" ]; then
          wait_failed "the bench container was created but has not started after ${grace}s"
          return 1
        fi
        ;;
      *) created_since='' ;;
    esac
    if [ "$BENCH_RESTARTS" -gt "$base_restarts" ]; then
      wait_failed "the bench container restarted while waiting"
      return 1
    fi
    if [ "$BENCH_HEALTH" = unhealthy ]; then
      wait_failed "the bench container is unhealthy"
      return 1
    fi
    if [ "$BENCH_STATE" = running ] && [ "$BENCH_HEALTH" = healthy ]; then
      echo "qa.sh: bench is healthy ($QA_BASE_URL)"
      return 0
    fi
    if bench_logged_failure; then
      wait_failed "the bench logged OC-QA-RESULT: FAIL"
      return 1
    fi
    elapsed=$((SECONDS - start))
    if [ "$elapsed" -ge "$timeout" ]; then
      wait_failed "timed out after ${timeout}s; the bench is $BENCH_STATE/$BENCH_HEALTH (QA_UP_TIMEOUT)"
      return 1
    fi
    if [ "$elapsed" -ge "$next_note" ]; then
      step=$(latest_step)
      echo "qa.sh: waiting for the bench (${elapsed}s, $BENCH_STATE/$BENCH_HEALTH)${step:+: $step}"
      next_note=$((elapsed + 30))
    fi
    sleep "$interval"
  done
}

cmd_up() {
  require_docker
  require_own_stack
  if ! compose up -d; then
    printf 'qa.sh: docker compose up failed\n' >&2
    show_results contracts-check \
      "$(compose ps -a -q contracts-check 2>/dev/null | head -n 1)"
    return 1
  fi
  cmd_wait
}

cmd_down() {
  require_docker
  require_own_stack
  # No --volumes, ever: destroying the data is docker/qa-teardown.sh.
  compose down
  echo "qa.sh: containers removed; volumes and credentials kept"
}

cmd_status() {
  require_docker
  compose ps
}

# Decode one word written by Python's shlex.quote(): bare characters from its
# safe set, single-quoted runs, and "'" for a literal single quote. Anything
# else is refused, so no part of a credentials line is ever evaluated as shell.
# Sets DECODED.
decode_word() {
  local rest=$1 out='' ch inside=0
  while [ -n "$rest" ]; do
    ch=${rest:0:1}
    rest=${rest:1}
    if [ "$inside" = 1 ]; then
      if [ "$ch" = "'" ]; then inside=0; else out+=$ch; fi
      continue
    fi
    case $ch in
      "'") inside=1 ;;
      '"')
        [ "${rest:0:2}" = "'\"" ] || return 1
        out+="'"
        rest=${rest:2}
        ;;
      [abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_@%+=:,./-])
        out+=$ch
        ;;
      *) return 1 ;;
    esac
  done
  [ "$inside" = 0 ] || return 1
  DECODED=$out
}

# Read the stack's credentials file out of the bench container and export what
# the live runner reads from it: QA_API_KEY, QA_API_SECRET and QA_SITE. Nothing
# else in the file (the Administrator password, say) reaches the runner. The
# content is held in memory only: it is never printed, written to disk, or
# traced.
load_credentials() {
  local content line name key='' secret='' site=''
  local seen_key=0 seen_secret=0 seen_site=0
  set +x
  # Whatever the caller had in these must not stand in for the stack's values.
  unset QA_API_KEY QA_API_SECRET QA_SITE
  if ! content=$(compose exec -T bench cat "$CREDENTIALS_PATH" </dev/null); then
    die "cannot read $CREDENTIALS_PATH in the bench container"
  fi
  while IFS= read -r line; do
    case $line in
      QA_API_KEY=* | QA_API_SECRET=* | QA_SITE=*) ;;
      *) continue ;;
    esac
    name=${line%%=*}
    decode_word "${line#*=}" \
      || die "credentials entry $name in the bench container is not a shlex-quoted word"
    case $name in
      QA_API_KEY)
        [ "$seen_key" = 0 ] || die "$name appears more than once in $CREDENTIALS_PATH"
        seen_key=1
        key=$DECODED
        ;;
      QA_API_SECRET)
        [ "$seen_secret" = 0 ] || die "$name appears more than once in $CREDENTIALS_PATH"
        seen_secret=1
        secret=$DECODED
        ;;
      QA_SITE)
        [ "$seen_site" = 0 ] || die "$name appears more than once in $CREDENTIALS_PATH"
        seen_site=1
        site=$DECODED
        ;;
    esac
  done < <(printf '%s\n' "$content")
  unset content line DECODED
  if [ -z "$key" ] || [ -z "$secret" ]; then
    die "$CREDENTIALS_PATH lacks a non-empty QA_API_KEY or QA_API_SECRET"
  fi
  export QA_API_KEY=$key QA_API_SECRET=$secret
  if [ -n "$site" ]; then
    export QA_SITE=$site
  fi
}

cmd_test() {
  local contracts ignored='' var
  require_docker
  require_own_stack
  command -v uv >/dev/null 2>&1 \
    || die "uv is not on PATH (https://docs.astral.sh/uv/)"
  find_bench
  if [ "$BENCH_STATE" != running ] || [ "$BENCH_HEALTH" != healthy ]; then
    die "the bench is $BENCH_STATE/$BENCH_HEALTH, not healthy; run 'qa.sh wait'"
  fi
  # The runner reads OC_API_KEY, OC_API_SECRET and OC_SITE before the QA_*
  # values, and OC_BASE for the server, so values left in the caller's
  # environment would send records somewhere other than the stack just checked.
  for var in OC_API_KEY OC_API_SECRET OC_SITE; do
    if [ "${!var:+set}" = set ]; then ignored="$ignored $var"; fi
  done
  if [ -n "${OC_BASE:-}" ] && [ "$OC_BASE" != "$QA_BASE_URL" ]; then
    ignored="$ignored OC_BASE"
  fi
  if [ -n "$ignored" ]; then
    printf 'qa.sh: ignoring%s: test always targets %s\n' "$ignored" "$QA_BASE_URL" >&2
  fi
  # The runner needs none of the compose inputs either.
  unset OC_API_KEY OC_API_SECRET OC_SITE QA_ADMIN_PASSWORD QA_DB_ROOT_PASSWORD
  export OC_BASE=$QA_BASE_URL
  # Compose reads OC_CONTRACTS_DIR, the live runner reads CONTRACTS_DIR. A
  # relative OC_CONTRACTS_DIR means the same directory to both: compose
  # resolves it from the compose file's directory. A relative CONTRACTS_DIR is
  # taken from where it was typed, as the runner starts in the code leg root.
  contracts=${CONTRACTS_DIR:-}
  if [ -z "$contracts" ] && [ -n "${OC_CONTRACTS_DIR:-}" ]; then
    case $OC_CONTRACTS_DIR in
      /*) contracts=$OC_CONTRACTS_DIR ;;
      *) contracts=$SCRIPT_DIR/$OC_CONTRACTS_DIR ;;
    esac
  fi
  case $contracts in
    '' | /*) ;;
    *) contracts=$PWD/$contracts ;;
  esac
  # Say so before any credentials are read or records are created, rather than
  # leaving a traceback from the runner. This mirrors
  # open_chart.intake.core.contracts_dir: the code leg's own contracts/ first,
  # then CONTRACTS_DIR, then the spec leg beside the code leg in an assembly
  # checkout.
  if [ -d "$CODE_ROOT/contracts" ]; then
    contracts=$CODE_ROOT/contracts
  elif [ -z "$contracts" ]; then
    contracts=$CODE_ROOT/../spec/contracts
  fi
  [ -s "$contracts/intake-census.yaml" ] \
    || die "no intake-census.yaml in $contracts; set OC_CONTRACTS_DIR (or CONTRACTS_DIR) to the contracts/ directory of the openChart spec leg"
  CONTRACTS_DIR=$(cd -P -- "$contracts" && pwd -P)
  export CONTRACTS_DIR
  load_credentials
  cd "$CODE_ROOT"
  exec uv run scripts/run_p4_live.py
}

main() {
  local sub=${1:-help}
  case $sub in
    help | -h | --help)
      usage
      return 0
      ;;
    up | wait | test | down | status)
      [ "$#" -le 1 ] || usage_error "'$sub' takes no arguments"
      "cmd_$sub"
      ;;
    *) usage_error "unknown command '$sub'" ;;
  esac
}

main "$@"

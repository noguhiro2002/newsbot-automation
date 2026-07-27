#!/usr/bin/env bash
set -euo pipefail
umask 077

usage() {
  cat <<'USAGE'
Run one Newsbot scheduled task from cron.

Usage:
  scripts/run_cron_task.sh [options] generate-review
  scripts/run_cron_task.sh [options] prepare-weekly
  scripts/run_cron_task.sh generate-review
  scripts/run_cron_task.sh prepare-weekly

Options:
  --smoke-test    Validate cron environment without running the task.
  --skip-codex    For generate-review, write the prompt but do not call Codex or submit to Discord.
  --force         For generate-review, ignore the every-days gate.
  --help          Show this help.

This script loads .env, applies interval gating for generate-review, and
executes the configured Python command from the repository root.
USAGE
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
default_app_dir="$(cd "$script_dir/.." && pwd)"
app_dir="${NEWSBOT_APP_DIR:-$default_app_dir}"
env_file="${NEWSBOT_ENV_FILE:-$app_dir/.env}"

load_env_file() {
  local path="$1"
  local raw_line key value

  [[ -f "$path" ]] || return 0

  while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
    raw_line="${raw_line%$'\r'}"
    [[ "$raw_line" =~ ^[[:space:]]*$ ]] && continue
    [[ "$raw_line" =~ ^[[:space:]]*# ]] && continue
    [[ "$raw_line" == *"="* ]] || continue

    key="${raw_line%%=*}"
    value="${raw_line#*=}"
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"

    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    if [[ "${#value}" -ge 2 ]]; then
      if [[ "${value:0:1}" == "\"" && "${value: -1}" == "\"" ]]; then
        value="${value:1:${#value}-2}"
      elif [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
        value="${value:1:${#value}-2}"
      fi
    fi
    export "$key=$value"
  done <"$path"
}

load_env_file "$env_file"
if [[ -f "$env_file" ]]; then
  chmod 0600 "$env_file"
fi

if [[ -n "${NEWSBOT_CODEX_BIN:-}" ]]; then
  codex_bin_dir="$(dirname "$NEWSBOT_CODEX_BIN")"
  if [[ -d "$codex_bin_dir" ]]; then
    export PATH="$codex_bin_dir:$PATH"
  fi
fi

smoke_test=0
skip_codex=0
force_run=0
task=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --smoke-test)
      smoke_test=1
      shift
      ;;
    --skip-codex)
      skip_codex=1
      shift
      ;;
    --force)
      force_run=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    generate-review|prepare-weekly)
      task="$1"
      shift
      ;;
    *)
      echo "Unknown option or task: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$task" != "generate-review" && "$task" != "prepare-weekly" ]]; then
  usage >&2
  exit 2
fi
if [[ "$skip_codex" -eq 1 && "$task" != "generate-review" ]]; then
  echo "--skip-codex is only valid for generate-review." >&2
  exit 2
fi

app_dir="${NEWSBOT_APP_DIR:-$app_dir}"
python_bin="${NEWSBOT_PYTHON_BIN:-$app_dir/.venv/bin/python}"
state_dir="${NEWSBOT_CRON_STATE_DIR:-$app_dir/data/cron}"

if [[ ! -d "$app_dir" ]]; then
  echo "App directory not found: $app_dir" >&2
  exit 1
fi
if [[ ! -x "$python_bin" ]]; then
  echo "Python binary not executable: $python_bin" >&2
  exit 1
fi

mkdir -p -m 0700 "$state_dir"
chmod 0700 "$state_dir"

run_smoke_test() {
  echo "task=$task"
  echo "app_dir=$app_dir"
  echo "python_bin=$python_bin"
  echo "state_dir=$state_dir"
  "$python_bin" -c 'import newsbot, discord; print("python imports ok")'
  if [[ "$task" == "generate-review" ]]; then
    echo "topic=${NEWSBOT_GENERATE_REVIEW_TOPIC:-lab_automation}"
    echo "cadence=${NEWSBOT_GENERATE_REVIEW_CADENCE:-weekly}"
    echo "every_days=${NEWSBOT_GENERATE_REVIEW_EVERY_DAYS:-2}"
    if [[ -n "${NEWSBOT_CODEX_BIN:-}" ]]; then
      echo "codex_bin=$NEWSBOT_CODEX_BIN"
      command -v node
      "$NEWSBOT_CODEX_BIN" --version
    else
      command -v codex
      codex --version
    fi
  fi
}

run_generate_review() {
  local topic cadence every_days state_file lock_file now last next_due codex_bin_args generate_args
  topic="${NEWSBOT_GENERATE_REVIEW_TOPIC:-lab_automation}"
  cadence="${NEWSBOT_GENERATE_REVIEW_CADENCE:-weekly}"
  every_days="${NEWSBOT_GENERATE_REVIEW_EVERY_DAYS:-2}"
  state_file="$state_dir/generate-review.last_success"
  lock_file="$state_dir/generate-review.lock"

  if ! [[ "$every_days" =~ ^[0-9]+$ ]]; then
    echo "NEWSBOT_GENERATE_REVIEW_EVERY_DAYS must be a non-negative integer: $every_days" >&2
    exit 1
  fi

  (
    flock -n 9 || {
      echo "generate-review is already running; skipping"
      exit 0
    }

    now="$(date +%s)"
    if [[ "$force_run" -eq 0 && "$every_days" -gt 0 && -s "$state_file" ]]; then
      last="$(cat "$state_file")"
      if [[ "$last" =~ ^[0-9]+$ ]]; then
        next_due=$((last + every_days * 86400))
        if [[ "$now" -lt "$next_due" ]]; then
          echo "generate-review is not due yet; last_success=$last every_days=$every_days"
          exit 0
        fi
      fi
    fi

    codex_bin_args=()
    if [[ -n "${NEWSBOT_CODEX_BIN:-}" ]]; then
      codex_bin_args=(--codex-bin "$NEWSBOT_CODEX_BIN")
    fi
    generate_args=(
      --topic "$topic"
      --cadence "$cadence"
    )
    if [[ "$skip_codex" -eq 1 ]]; then
      generate_args+=(--skip-codex)
    else
      generate_args+=(--submit-review)
    fi

    cd "$app_dir"
    "$python_bin" scripts/generate_payload_openai.py \
      "${generate_args[@]}" \
      "${codex_bin_args[@]}"
    if [[ "$skip_codex" -eq 0 ]]; then
      date +%s >"$state_file.tmp"
      mv "$state_file.tmp" "$state_file"
    fi
  ) 9>"$lock_file"
}

run_prepare_weekly() {
  local topic cadence lock_file
  topic="${NEWSBOT_GENERATE_REVIEW_TOPIC:-lab_automation}"
  cadence="${NEWSBOT_GENERATE_REVIEW_CADENCE:-weekly}"
  lock_file="$state_dir/prepare-weekly.lock"

  (
    flock -n 9 || {
      echo "prepare-weekly is already running; skipping"
      exit 0
    }
    cd "$app_dir"
    "$python_bin" -m newsbot.cli prepare-weekly-publish \
      --topic "$topic" \
      --cadence "$cadence"
  ) 9>"$lock_file"
}

case "$task" in
  generate-review)
    if [[ "$smoke_test" -eq 1 ]]; then
      run_smoke_test
    else
      run_generate_review
    fi
    ;;
  prepare-weekly)
    if [[ "$smoke_test" -eq 1 ]]; then
      run_smoke_test
    else
      run_prepare_weekly
    fi
    ;;
esac

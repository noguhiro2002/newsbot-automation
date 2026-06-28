#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Install Newsbot cron jobs.

Usage:
  scripts/install_cron_jobs.sh [options]

Options:
  --app-dir PATH       Repository path. Default: this script's repository root.
  --user USER          Linux user that cron should run commands as. Overrides NEWSBOT_CRON_USER.
  --python-bin PATH    Python binary. Overrides NEWSBOT_PYTHON_BIN.
  --cron-file PATH     Cron file path. Default: /etc/cron.d/newsbot-automation.
  --dry-run            Print the cron file instead of installing it.
  --help               Show this help.

.env defaults:
  NEWSBOT_CRON_USER                 SUDO_USER when available, otherwise root
  NEWSBOT_CRON_TZ                   Asia/Tokyo
  NEWSBOT_GENERATE_REVIEW_CRON      0 8 * * *
  NEWSBOT_GENERATE_REVIEW_EVERY_DAYS 2
  NEWSBOT_GENERATE_REVIEW_TOPIC     lab_automation
  NEWSBOT_GENERATE_REVIEW_CADENCE   weekly
  NEWSBOT_PREPARE_WEEKLY_CRON       0 8 * * 1
  NEWSBOT_PYTHON_BIN                APP_DIR/.venv/bin/python
USAGE
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
default_app_dir="$(cd "$script_dir/.." && pwd)"

app_dir="$default_app_dir"
cron_user=""
python_bin=""
cron_file="/etc/cron.d/newsbot-automation"
dry_run=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir)
      app_dir="${2:-}"
      shift 2
      ;;
    --user)
      cron_user="${2:-}"
      shift 2
      ;;
    --python-bin)
      python_bin="${2:-}"
      shift 2
      ;;
    --cron-file)
      cron_file="${2:-}"
      shift 2
      ;;
    --dry-run)
      dry_run=1
      shift
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

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

shell_quote() {
  printf "%q" "$1"
}

validate_cron_schedule() {
  local name="$1"
  local schedule="$2"
  local field_count
  field_count="$(awk '{print NF}' <<<"$schedule")"
  if [[ "$field_count" -ne 5 ]]; then
    echo "$name must contain exactly 5 cron fields: $schedule" >&2
    exit 1
  fi
}

if [[ ! -d "$app_dir" ]]; then
  echo "App directory not found: $app_dir" >&2
  exit 1
fi

env_file="$app_dir/.env"
if [[ ! -f "$env_file" ]]; then
  echo ".env not found: $env_file" >&2
  exit 1
fi

load_env_file "$env_file"

cron_tz="${NEWSBOT_CRON_TZ:-Asia/Tokyo}"
generate_schedule="${NEWSBOT_GENERATE_REVIEW_CRON:-0 8 * * *}"
prepare_schedule="${NEWSBOT_PREPARE_WEEKLY_CRON:-0 8 * * 1}"
generate_every_days="${NEWSBOT_GENERATE_REVIEW_EVERY_DAYS:-2}"
generate_topic="${NEWSBOT_GENERATE_REVIEW_TOPIC:-lab_automation}"
generate_cadence="${NEWSBOT_GENERATE_REVIEW_CADENCE:-weekly}"
cron_user="${cron_user:-${NEWSBOT_CRON_USER:-${SUDO_USER:-root}}}"
python_bin="${python_bin:-${NEWSBOT_PYTHON_BIN:-$app_dir/.venv/bin/python}}"
codex_log="${NEWSBOT_CODEX_LOG:-$app_dir/logs/newsbot-codex.log}"
weekly_log="${NEWSBOT_WEEKLY_LOG:-$app_dir/logs/newsbot-weekly.log}"
state_dir="${NEWSBOT_CRON_STATE_DIR:-$app_dir/data/cron}"
runner="$app_dir/scripts/run_cron_task.sh"

validate_cron_schedule NEWSBOT_GENERATE_REVIEW_CRON "$generate_schedule"
validate_cron_schedule NEWSBOT_PREPARE_WEEKLY_CRON "$prepare_schedule"

if ! [[ "$generate_every_days" =~ ^[0-9]+$ ]]; then
  echo "NEWSBOT_GENERATE_REVIEW_EVERY_DAYS must be a non-negative integer: $generate_every_days" >&2
  exit 1
fi
if [[ ! -x "$python_bin" ]]; then
  echo "Python binary not executable: $python_bin" >&2
  exit 1
fi
if [[ ! -x "$runner" ]]; then
  echo "Cron runner not executable: $runner" >&2
  exit 1
fi
if ! id "$cron_user" >/dev/null 2>&1; then
  echo "Linux user not found: $cron_user" >&2
  exit 1
fi

if [[ "$dry_run" -eq 0 && "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Installing to $cron_file requires root, for example with sudo." >&2
  exit 1
fi

if [[ "$dry_run" -eq 0 ]]; then
  if [[ "$cron_user" == "root" ]]; then
    "$python_bin" -c 'import sys' >/dev/null
  elif command -v runuser >/dev/null 2>&1; then
    runuser -u "$cron_user" -- "$python_bin" -c 'import sys' >/dev/null
  else
    echo "runuser command not found; cannot verify cron-user Python execution." >&2
    exit 1
  fi

  mkdir -p "$app_dir/logs" "$state_dir"
  touch "$codex_log" "$weekly_log"
  if [[ "$cron_user" != "root" ]]; then
    cron_group="$(id -gn "$cron_user")"
    chown "$cron_user:$cron_group" "$app_dir/logs" "$codex_log" "$weekly_log" "$state_dir"
  fi
fi

quoted_app_dir="$(shell_quote "$app_dir")"
quoted_python_bin="$(shell_quote "$python_bin")"
quoted_runner="$(shell_quote "$runner")"
quoted_codex_log="$(shell_quote "$codex_log")"
quoted_weekly_log="$(shell_quote "$weekly_log")"

tmp_file="$(mktemp)"
cat >"$tmp_file" <<CRON
# Generated by Newsbot install_cron_jobs.sh.
# Edit $env_file, then rerun the installer instead of editing this file directly.
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
CRON_TZ=$cron_tz

# Trigger daily at 08:00 JST by default; run_cron_task.sh enforces NEWSBOT_GENERATE_REVIEW_EVERY_DAYS=$generate_every_days.
$generate_schedule $cron_user cd $quoted_app_dir && NEWSBOT_APP_DIR=$quoted_app_dir NEWSBOT_PYTHON_BIN=$quoted_python_bin $quoted_runner generate-review >> $quoted_codex_log 2>&1

# Prepare the weekly final-review workflow every Monday at 08:00 JST by default.
$prepare_schedule $cron_user cd $quoted_app_dir && NEWSBOT_APP_DIR=$quoted_app_dir NEWSBOT_PYTHON_BIN=$quoted_python_bin $quoted_runner prepare-weekly >> $quoted_weekly_log 2>&1
CRON

if [[ "$dry_run" -eq 1 ]]; then
  cat "$tmp_file"
  rm -f "$tmp_file"
else
  install -m 0644 "$tmp_file" "$cron_file"
  rm -f "$tmp_file"
  echo "Installed cron jobs: $cron_file"
  echo "User: $cron_user"
  echo "Timezone: $cron_tz"
  echo "Generate review: $generate_schedule, every_days=$generate_every_days, topic=$generate_topic, cadence=$generate_cadence"
  echo "Prepare weekly: $prepare_schedule"
  echo "Logs: $codex_log, $weekly_log"
fi

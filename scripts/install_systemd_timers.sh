#!/usr/bin/env bash
set -euo pipefail
umask 077

usage() {
  cat <<'USAGE'
Install Newsbot systemd timers, SQLite backup, and log rotation.

Usage:
  sudo scripts/install_systemd_timers.sh [options]
  scripts/install_systemd_timers.sh --dry-run [options]

Options:
  --app-dir PATH              Repository path. Default: this script's repository root.
  --user USER                 Service user. Default: NEWSBOT_CRON_USER, SUDO_USER, or root.
  --group GROUP               Service group. Default: primary group of --user.
  --python-bin PATH           Python binary. Default: NEWSBOT_PYTHON_BIN or APP_DIR/.venv/bin/python.
  --generate-calendar EXPR    Default: Mon *-*-* 06:00:00 Asia/Tokyo
  --prepare-calendar EXPR     Default: Mon *-*-* 09:00:00 Asia/Tokyo
  --backup-calendar EXPR      Default: *-*-* 03:30:00 Asia/Tokyo
  --backup-keep COUNT         SQLite backup generations. Default: 14.
  --dry-run                   Validate and print files without installing.
  --help                      Show this help.

The installer enables all timers, creates the first SQLite backup, and moves
/etc/cron.d/newsbot-automation to a timestamped .disabled file to prevent
duplicate execution.
USAGE
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
default_app_dir="$(cd "$script_dir/.." && pwd)"
app_dir="$default_app_dir"
run_user=""
run_group=""
python_bin=""
generate_calendar=""
prepare_calendar=""
backup_calendar=""
backup_keep=""
dry_run=0
cron_file="/etc/cron.d/newsbot-automation"
unit_dir="/etc/systemd/system"
logrotate_file="/etc/logrotate.d/newsbot-automation"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir) app_dir="${2:-}"; shift 2 ;;
    --user) run_user="${2:-}"; shift 2 ;;
    --group) run_group="${2:-}"; shift 2 ;;
    --python-bin) python_bin="${2:-}"; shift 2 ;;
    --generate-calendar) generate_calendar="${2:-}"; shift 2 ;;
    --prepare-calendar) prepare_calendar="${2:-}"; shift 2 ;;
    --backup-calendar) backup_calendar="${2:-}"; shift 2 ;;
    --backup-keep) backup_keep="${2:-}"; shift 2 ;;
    --dry-run) dry_run=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

load_env_file() {
  local path="$1" raw_line key value
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
      if [[ "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
        value="${value:1:${#value}-2}"
      elif [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
        value="${value:1:${#value}-2}"
      fi
    fi
    export "$key=$value"
  done <"$path"
}

if [[ ! -d "$app_dir" ]]; then
  echo "App directory not found: $app_dir" >&2
  exit 1
fi
case "$app_dir" in
  *[$'\t\r\n ']* ) echo "App directory must not contain whitespace: $app_dir" >&2; exit 1 ;;
esac

env_file="$app_dir/.env"
if [[ ! -f "$env_file" ]]; then
  echo ".env not found: $env_file" >&2
  exit 1
fi
load_env_file "$env_file"

run_user="${run_user:-${NEWSBOT_CRON_USER:-${SUDO_USER:-root}}}"
if ! id "$run_user" >/dev/null 2>&1; then
  echo "Linux user not found: $run_user" >&2
  exit 1
fi
run_group="${run_group:-$(id -gn "$run_user")}"
if ! getent group "$run_group" >/dev/null 2>&1; then
  echo "Linux group not found: $run_group" >&2
  exit 1
fi
user_home="$(getent passwd "$run_user" | awk -F: '{print $6}')"
python_bin="${python_bin:-${NEWSBOT_PYTHON_BIN:-$app_dir/.venv/bin/python}}"
generate_calendar="${generate_calendar:-${NEWSBOT_GENERATE_REVIEW_ON_CALENDAR:-Mon *-*-* 06:00:00 Asia/Tokyo}}"
prepare_calendar="${prepare_calendar:-${NEWSBOT_PREPARE_WEEKLY_ON_CALENDAR:-Mon *-*-* 09:00:00 Asia/Tokyo}}"
backup_calendar="${backup_calendar:-${NEWSBOT_SQLITE_BACKUP_ON_CALENDAR:-*-*-* 03:30:00 Asia/Tokyo}}"
backup_keep="${backup_keep:-${NEWSBOT_SQLITE_BACKUP_KEEP:-14}}"
notification_retry_seconds="${NEWSBOT_JOB_NOTIFICATION_RETRY_SECONDS:-300}"
runner="$app_dir/scripts/run_cron_task.sh"
backup_script="$app_dir/scripts/backup_sqlite.py"
backup_dir="${NEWSBOT_SQLITE_BACKUP_DIR:-$app_dir/backups/sqlite}"

if [[ ! -x "$python_bin" ]]; then
  echo "Python binary not executable: $python_bin" >&2
  exit 1
fi
if [[ ! -x "$runner" ]]; then
  echo "Task runner not executable: $runner" >&2
  exit 1
fi
if [[ ! -x "$backup_script" ]]; then
  echo "Backup script not executable: $backup_script" >&2
  exit 1
fi
if ! [[ "$backup_keep" =~ ^[1-9][0-9]*$ ]]; then
  echo "Backup keep count must be a positive integer: $backup_keep" >&2
  exit 1
fi
if ! [[ "$notification_retry_seconds" =~ ^[0-9]+$ ]] || (( notification_retry_seconds < 60 || notification_retry_seconds > 3600 )); then
  echo "NEWSBOT_JOB_NOTIFICATION_RETRY_SECONDS must be between 60 and 3600." >&2
  exit 1
fi
for calendar in "$generate_calendar" "$prepare_calendar" "$backup_calendar"; do
  systemd-analyze calendar "$calendar" >/dev/null
done

work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

cat >"$work_dir/newsbot-generate-review.service" <<UNIT
[Unit]
Description=Newsbot generate Discord review candidates
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$app_dir
EnvironmentFile=$env_file
Environment=HOME=$user_home
Environment=NEWSBOT_APP_DIR=$app_dir
Environment=NEWSBOT_PYTHON_BIN=$python_bin
ExecStart=$runner generate-review
User=$run_user
Group=$run_group
UMask=0077
TimeoutStartSec=12h
UNIT

cat >"$work_dir/newsbot-generate-review.timer" <<UNIT
[Unit]
Description=Generate Newsbot review candidates on schedule

[Timer]
OnCalendar=$generate_calendar
Persistent=true
AccuracySec=1min
Unit=newsbot-generate-review.service

[Install]
WantedBy=timers.target
UNIT

cat >"$work_dir/newsbot-prepare-weekly.service" <<UNIT
[Unit]
Description=Newsbot prepare weekly publish review
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$app_dir
EnvironmentFile=$env_file
Environment=HOME=$user_home
Environment=NEWSBOT_APP_DIR=$app_dir
Environment=NEWSBOT_PYTHON_BIN=$python_bin
ExecStart=$runner prepare-weekly
User=$run_user
Group=$run_group
UMask=0077
TimeoutStartSec=30min
UNIT

cat >"$work_dir/newsbot-prepare-weekly.timer" <<UNIT
[Unit]
Description=Prepare Newsbot weekly publish review on schedule

[Timer]
OnCalendar=$prepare_calendar
Persistent=true
AccuracySec=1min
Unit=newsbot-prepare-weekly.service

[Install]
WantedBy=timers.target
UNIT

cat >"$work_dir/newsbot-sqlite-backup.service" <<UNIT
[Unit]
Description=Newsbot verified SQLite online backup

[Service]
Type=oneshot
WorkingDirectory=$app_dir
ExecStart=$python_bin $backup_script --db $app_dir/data/newsbot.sqlite --backup-dir $backup_dir --keep $backup_keep
User=$run_user
Group=$run_group
UMask=0077
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=30min
NoNewPrivileges=true
PrivateTmp=true
UNIT

cat >"$work_dir/newsbot-sqlite-backup.timer" <<UNIT
[Unit]
Description=Back up Newsbot SQLite database daily

[Timer]
OnCalendar=$backup_calendar
Persistent=true
AccuracySec=1min
Unit=newsbot-sqlite-backup.service

[Install]
WantedBy=timers.target
UNIT

cat >"$work_dir/newsbot-job-notifications.service" <<UNIT
[Unit]
Description=Deliver queued Newsbot job alerts to the Discord review channel
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$app_dir
EnvironmentFile=$env_file
ExecStart=$python_bin $app_dir/scripts/run_scheduled_job.py flush-notifications
User=$run_user
Group=$run_group
UMask=0077
TimeoutStartSec=15min
UNIT

cat >"$work_dir/newsbot-job-notifications.timer" <<UNIT
[Unit]
Description=Retry undelivered Newsbot job notifications

[Timer]
OnBootSec=2min
OnUnitInactiveSec=${notification_retry_seconds}s
Unit=newsbot-job-notifications.service

[Install]
WantedBy=timers.target
UNIT

cat >"$work_dir/newsbot-automation.logrotate" <<LOGROTATE
$app_dir/logs/*.log {
    weekly
    rotate 12
    maxsize 10M
    compress
    delaycompress
    missingok
    notifempty
    create 0600 $run_user $run_group
    su $run_user $run_group
}
LOGROTATE

systemd-analyze verify \
  "$work_dir/newsbot-generate-review.service" \
  "$work_dir/newsbot-generate-review.timer" \
  "$work_dir/newsbot-prepare-weekly.service" \
  "$work_dir/newsbot-prepare-weekly.timer" \
  "$work_dir/newsbot-sqlite-backup.service" \
  "$work_dir/newsbot-sqlite-backup.timer" \
  "$work_dir/newsbot-job-notifications.service" \
  "$work_dir/newsbot-job-notifications.timer"

if [[ "$dry_run" -eq 1 ]]; then
  for file in "$work_dir"/*; do
    echo "===== $(basename "$file") ====="
    cat "$file"
  done
  exit 0
fi

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Installing system units requires root, for example with sudo." >&2
  exit 1
fi
if ! command -v logrotate >/dev/null 2>&1; then
  echo "logrotate command not found." >&2
  exit 1
fi

install -d -m 0700 -o "$run_user" -g "$run_group" "$backup_dir" "$app_dir/logs" "$app_dir/data/cron"
chown "$run_user:$run_group" "$env_file"
chmod 0600 "$env_file"

for unit in "$work_dir"/*.service "$work_dir"/*.timer; do
  install -m 0644 "$unit" "$unit_dir/$(basename "$unit")"
done
install -m 0644 "$work_dir/newsbot-automation.logrotate" "$logrotate_file"
logrotate --debug "$logrotate_file" >/dev/null

systemctl daemon-reload
systemctl enable --now \
  newsbot-generate-review.timer \
  newsbot-prepare-weekly.timer \
  newsbot-sqlite-backup.timer \
  newsbot-job-notifications.timer

systemctl start newsbot-sqlite-backup.service

if [[ -f "$cron_file" ]]; then
  disabled_cron="${cron_file}.disabled.$(date -u +%Y%m%dT%H%M%SZ)"
  mv "$cron_file" "$disabled_cron"
  echo "Disabled old cron file: $disabled_cron"
  systemctl reload cron 2>/dev/null || true
fi

echo "Installed Newsbot systemd timers and log rotation."
echo "SQLite backups: $backup_dir (keep=$backup_keep)"
systemctl list-timers --all \
  newsbot-generate-review.timer \
  newsbot-prepare-weekly.timer \
  newsbot-sqlite-backup.timer \
  newsbot-job-notifications.timer \
  --no-pager

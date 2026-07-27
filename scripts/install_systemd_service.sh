#!/usr/bin/env bash
set -euo pipefail
umask 077

usage() {
  cat <<'USAGE'
Install and start the Newsbot Discord Bot systemd service.

Usage:
  scripts/install_systemd_service.sh --env venv [options]
  scripts/install_systemd_service.sh --env conda [options]

Options:
  --env venv|conda          Python environment type. Required.
  --app-dir PATH            Repository path. Default: /opt/newsbot-automation
  --service-name NAME       systemd service name. Default: newsbot-discord-bot
  --user USER               Linux user running the service. Default: newsbot
  --group GROUP             Linux group running the service. Default: same as --user
  --python-bin PATH         Python binary. Defaults depend on --env.
  --help                    Show this help.

Examples:
  sudo scripts/install_systemd_service.sh --env venv --app-dir /opt/newsbot-automation
  sudo scripts/install_systemd_service.sh --env conda --python-bin /opt/miniconda3/envs/newsbot/bin/python
USAGE
}

env_type=""
app_dir="/opt/newsbot-automation"
service_name="newsbot-discord-bot"
run_user="newsbot"
run_group=""
python_bin=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)
      env_type="${2:-}"
      shift 2
      ;;
    --app-dir)
      app_dir="${2:-}"
      shift 2
      ;;
    --service-name)
      service_name="${2:-}"
      shift 2
      ;;
    --user)
      run_user="${2:-}"
      shift 2
      ;;
    --group)
      run_group="${2:-}"
      shift 2
      ;;
    --python-bin)
      python_bin="${2:-}"
      shift 2
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

if [[ "$env_type" != "venv" && "$env_type" != "conda" ]]; then
  echo "--env must be either 'venv' or 'conda'." >&2
  usage >&2
  exit 2
fi

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "This installer must be run as root, for example with sudo." >&2
  exit 1
fi

if [[ -z "$run_group" ]]; then
  run_group="$run_user"
fi

if [[ -z "$python_bin" ]]; then
  if [[ "$env_type" == "venv" ]]; then
    python_bin="$app_dir/.venv/bin/python"
  else
    python_bin="/opt/miniconda3/envs/newsbot/bin/python"
  fi
fi

if [[ ! -d "$app_dir" ]]; then
  echo "App directory not found: $app_dir" >&2
  exit 1
fi

if [[ ! -f "$app_dir/.env" ]]; then
  echo ".env not found: $app_dir/.env" >&2
  exit 1
fi

if [[ ! -x "$python_bin" ]]; then
  echo "Python binary not executable: $python_bin" >&2
  exit 1
fi

if ! id "$run_user" >/dev/null 2>&1; then
  echo "Linux user not found: $run_user" >&2
  echo "Create it first, for example: sudo useradd --system --home $app_dir --shell /usr/sbin/nologin $run_user" >&2
  exit 1
fi

if ! command -v runuser >/dev/null 2>&1; then
  echo "runuser command not found; cannot verify service-user Python execution." >&2
  exit 1
fi

if ! runuser -u "$run_user" -g "$run_group" -- "$python_bin" -c 'import sys' >/dev/null 2>&1; then
  resolved_python="$(readlink -f "$python_bin" 2>/dev/null || printf '%s' "$python_bin")"
  echo "Python binary is not executable by service user '$run_user': $python_bin" >&2
  echo "Resolved Python path: $resolved_python" >&2
  echo "If this venv was created from Python under a private home directory, recreate it from an accessible Python runtime or choose a service user that can execute that runtime." >&2
  exit 1
fi

chown "$run_user:$run_group" "$app_dir/.env"
chmod 0600 "$app_dir/.env"
for runtime_dir in data logs payloads reports; do
  install -d -m 0700 -o "$run_user" -g "$run_group" "$app_dir/$runtime_dir"
  chown -R "$run_user:$run_group" "$app_dir/$runtime_dir"
  chmod -R go-rwx "$app_dir/$runtime_dir"
done

service_path="/etc/systemd/system/${service_name}.service"
tmp_file="$(mktemp)"

cat >"$tmp_file" <<UNIT
[Unit]
Description=Newsbot Discord Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$app_dir
EnvironmentFile=$app_dir/.env
Environment=PYTHONUNBUFFERED=1
UMask=0077
ExecStart=$python_bin -m newsbot.cli run-discord-bot
Restart=always
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=30
User=$run_user
Group=$run_group

[Install]
WantedBy=multi-user.target
UNIT

install -m 0644 "$tmp_file" "$service_path"
rm -f "$tmp_file"

systemctl daemon-reload
systemctl enable --now "$service_name"

echo "Installed and started $service_name"
echo "Status: sudo systemctl status $service_name"
echo "Logs:   sudo journalctl -u $service_name -f"

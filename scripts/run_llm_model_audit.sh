#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'USAGE'
Run baseline or targeted LLM audit configurations without posting to Discord.

Usage:
  scripts/run_llm_model_audit.sh --period PERIOD [options]

Required:
  --period PERIOD       Fixed search period shared by every run.
                        Example: "2026-07-18 to 2026-07-25 (JST)"

Options:
  --execute             Actually run Codex. Without this flag, print the plan only.
  --repeats N           Runs per configuration. Default: 3
  --only NAME           Run only one configuration:
                        gpt55, luna-hybrid, terra, sol,
                        luna-master-high, luna-phase1-xhigh,
                        luna-phase1-xhigh-master-high, or
                        luna-all-phases-xhigh-master-high.
  --codex-timeout SEC   Timeout for each Codex phase. Default: 3600
  --paper-api-retmax N  Paper candidates fetched per API. Uses the app default if omitted.
  --stop-on-error       Stop after the first failed run. Default: continue and summarize.
  --help                Show this help.

Environment:
  NEWSBOT_AUDIT_PYTHON_BIN  Python executable. Default: <repo>/.venv/bin/python
  NEWSBOT_CODEX_BIN         Codex executable. Default: codex

Each payload remains under payloads/. Per-run console logs and a TSV manifest are
written under logs/model-audit/<session-id>/.
USAGE
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app_dir="$(cd "$script_dir/.." && pwd)"
python_bin="${NEWSBOT_AUDIT_PYTHON_BIN:-$app_dir/.venv/bin/python}"
codex_bin="${NEWSBOT_CODEX_BIN:-codex}"

period=""
repeats=3
only_config=""
codex_timeout=3600
paper_api_retmax=""
execute_runs=0
stop_on_error=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --period)
      [[ $# -ge 2 ]] || {
        echo "--period requires a value." >&2
        exit 2
      }
      period="$2"
      shift 2
      ;;
    --repeats)
      [[ $# -ge 2 ]] || {
        echo "--repeats requires a value." >&2
        exit 2
      }
      repeats="$2"
      shift 2
      ;;
    --only)
      [[ $# -ge 2 ]] || {
        echo "--only requires a value." >&2
        exit 2
      }
      only_config="$2"
      shift 2
      ;;
    --codex-timeout)
      [[ $# -ge 2 ]] || {
        echo "--codex-timeout requires a value." >&2
        exit 2
      }
      codex_timeout="$2"
      shift 2
      ;;
    --paper-api-retmax)
      [[ $# -ge 2 ]] || {
        echo "--paper-api-retmax requires a value." >&2
        exit 2
      }
      paper_api_retmax="$2"
      shift 2
      ;;
    --execute)
      execute_runs=1
      shift
      ;;
    --stop-on-error)
      stop_on_error=1
      shift
      ;;
    --help|-h)
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

if [[ -z "$period" ]]; then
  echo "--period is required so every model is evaluated against the same date range." >&2
  usage >&2
  exit 2
fi
if ! [[ "$repeats" =~ ^[1-9][0-9]*$ ]]; then
  echo "--repeats must be a positive integer: $repeats" >&2
  exit 2
fi
if ! [[ "$codex_timeout" =~ ^[1-9][0-9]*$ ]]; then
  echo "--codex-timeout must be a positive integer: $codex_timeout" >&2
  exit 2
fi
if [[ -n "$paper_api_retmax" ]] && ! [[ "$paper_api_retmax" =~ ^[1-9][0-9]*$ ]]; then
  echo "--paper-api-retmax must be a positive integer: $paper_api_retmax" >&2
  exit 2
fi

default_config_count=4
config_names=(
  gpt55
  luna-hybrid
  terra
  sol
  luna-master-high
  luna-phase1-xhigh
  luna-phase1-xhigh-master-high
  luna-all-phases-xhigh-master-high
)
phase_models=(
  gpt-5.5
  gpt-5.6-luna
  gpt-5.6-terra
  gpt-5.6-sol
  gpt-5.6-luna
  gpt-5.6-luna
  gpt-5.6-luna
  gpt-5.6-luna
)
phase_1_efforts=(high high high high high xhigh xhigh xhigh)
phase_2_efforts=(high high high high high high high xhigh)
phase_3_efforts=(high high high high high high high xhigh)
phase_4_efforts=(high high high high high high high xhigh)
master_models=(
  gpt-5.5
  gpt-5.6-terra
  gpt-5.6-terra
  gpt-5.6-sol
  gpt-5.6-terra
  gpt-5.6-terra
  gpt-5.6-terra
  gpt-5.6-terra
)
master_efforts=(high medium high high high medium high high)

if [[ -n "$only_config" ]]; then
  valid_config=0
  for config_name in "${config_names[@]}"; do
    if [[ "$config_name" == "$only_config" ]]; then
      valid_config=1
      break
    fi
  done
  if [[ "$valid_config" -eq 0 ]]; then
    echo "Unknown configuration: $only_config" >&2
    echo "Expected one of: ${config_names[*]}" >&2
    exit 2
  fi
fi

config_selected() {
  local index="$1"
  if [[ -n "$only_config" ]]; then
    [[ "${config_names[$index]}" == "$only_config" ]]
  else
    [[ "$index" -lt "$default_config_count" ]]
  fi
}

selected_count=$default_config_count
if [[ -n "$only_config" ]]; then
  selected_count=1
fi
total_runs=$((selected_count * repeats))
total_codex_turns=$((total_runs * 5))

echo "LLM model audit plan"
echo "  repository: $app_dir"
echo "  period: $period"
echo "  repeats per configuration: $repeats"
echo "  generator runs: $total_runs"
echo "  Codex exec turns: $total_codex_turns (4 phases + 1 master per run)"
echo "  Discord submission: disabled (--validate-only)"
for index in "${!config_names[@]}"; do
  name="${config_names[$index]}"
  config_selected "$index" || continue
  echo "  - $name: phase_1=${phase_models[$index]}/${phase_1_efforts[$index]}, phase_2=${phase_models[$index]}/${phase_2_efforts[$index]}, phase_3=${phase_models[$index]}/${phase_3_efforts[$index]}, phase_4=${phase_models[$index]}/${phase_4_efforts[$index]}, master=${master_models[$index]}/${master_efforts[$index]}"
done

if [[ "$execute_runs" -eq 0 ]]; then
  echo
  echo "Plan only. Add --execute to start the paid Codex runs."
  exit 0
fi

if [[ ! -x "$python_bin" ]]; then
  echo "Python executable not found: $python_bin" >&2
  exit 1
fi
if ! command -v "$codex_bin" >/dev/null 2>&1 && [[ ! -x "$codex_bin" ]]; then
  echo "Codex executable not found: $codex_bin" >&2
  exit 1
fi

session_id="$(date +%Y%m%d_%H%M%S_%6N)"
audit_root="$app_dir/logs/model-audit"
session_dir="$audit_root/$session_id"
manifest_path="$session_dir/runs.tsv"
if ! mkdir -p "$session_dir"; then
  echo "Could not create session directory: $session_dir" >&2
  exit 1
fi

if ! exec 9>"$audit_root/audit.lock"; then
  echo "Could not create audit lock: $audit_root/audit.lock" >&2
  exit 1
fi
if ! flock -n 9; then
  echo "Another model audit is already running." >&2
  exit 1
fi

if ! printf 'sequence\tconfiguration\treplicate\tstatus\texit_code\toutput\tlog\n' >"$manifest_path"; then
  echo "Could not create run manifest: $manifest_path" >&2
  exit 1
fi

echo
echo "Preflight"
"$python_bin" -m py_compile "$app_dir/scripts/generate_payload_openai.py" || exit 1
"$codex_bin" --version || exit 1
echo "  session: $session_id"
echo "  manifest: $manifest_path"

failures=0
sequence=0

for index in "${!config_names[@]}"; do
  name="${config_names[$index]}"
  config_selected "$index" || continue

  for ((replicate = 1; replicate <= repeats; replicate++)); do
    sequence=$((sequence + 1))
    timestamp="$(date +%Y%m%d_%H%M%S_%6N)"
    output_path="$app_dir/payloads/lab_automation_weekly_${timestamp}.json"
    log_path="$session_dir/$(printf '%02d' "$sequence")_${name}_r${replicate}.log"

    command_args=(
      "$python_bin"
      "$app_dir/scripts/generate_payload_openai.py"
      --topic lab_automation
      --cadence weekly
      --period "$period"
      --output "$output_path"
      --codex-bin "$codex_bin"
      --codex-timeout "$codex_timeout"
      --phase-model "phase_1=${phase_models[$index]}"
      --phase-model "phase_2=${phase_models[$index]}"
      --phase-model "phase_3=${phase_models[$index]}"
      --phase-model "phase_4=${phase_models[$index]}"
      --phase-reasoning "phase_1=${phase_1_efforts[$index]}"
      --phase-reasoning "phase_2=${phase_2_efforts[$index]}"
      --phase-reasoning "phase_3=${phase_3_efforts[$index]}"
      --phase-reasoning "phase_4=${phase_4_efforts[$index]}"
      --master-model "${master_models[$index]}"
      --master-reasoning-effort "${master_efforts[$index]}"
      --validate-only
    )
    if [[ -n "$paper_api_retmax" ]]; then
      command_args+=(--paper-api-retmax "$paper_api_retmax")
    fi

    echo
    echo "[$sequence/$total_runs] configuration=$name replicate=$replicate"
    echo "  output: $output_path"
    echo "  log: $log_path"

    (
      cd "$app_dir" || exit 1
      "${command_args[@]}"
    ) 2>&1 | tee "$log_path"
    exit_code=${PIPESTATUS[0]}

    if [[ "$exit_code" -eq 0 ]]; then
      status=success
    else
      status=failed
      failures=$((failures + 1))
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$sequence" "$name" "$replicate" "$status" "$exit_code" "$output_path" "$log_path" \
      >>"$manifest_path"

    echo "  result: $status (exit_code=$exit_code)"
    if [[ "$exit_code" -ne 0 && "$stop_on_error" -eq 1 ]]; then
      echo "Stopping after the first failure. See: $manifest_path" >&2
      exit "$exit_code"
    fi
  done
done

echo
echo "Audit execution finished"
echo "  successful: $((total_runs - failures))"
echo "  failed: $failures"
echo "  manifest: $manifest_path"
echo "  notebook: $app_dir/analysis/llm-model-audit/llm_model_audit.ipynb"

if [[ "$failures" -gt 0 ]]; then
  exit 1
fi

#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'USAGE'
Run a paired, selection-only Master model audit.

Usage:
  scripts/run_master_paired_audit.sh \
    --period PERIOD \
    --source-run RUN_1 \
    --source-run RUN_2 \
    [options]

Required:
  --period PERIOD       Fixed period used by both source runs.
  --source-run RUN      Source run stem or final JSON. Specify exactly twice.

Options:
  --execute             Run the paid Codex turns. Default: plan only.
  --seed N              Candidate-order seed. Repeatable.
                        Default: 101, 202, 303
  --only NAME           Run one Master condition:
                        terra-medium, terra-high,
                        sol-medium, sol-high,
                        gpt55-high, or luna-high.
  --max-items N         Maximum Master selections. Default: 30
  --codex-timeout SEC   Timeout per Master turn. Default: 5400
  --stop-on-error       Stop after the first failed Master turn.
  --help                Show this help.

Environment:
  NEWSBOT_MASTER_AUDIT_PYTHON_BIN
      Python executable. Default: <repo>/.venv/bin/python
  NEWSBOT_CODEX_BIN
      Codex executable. Default: codex

Paired design:
  For each seed, one fixed, shuffled candidate-pool JSON is generated once.
  The exact same file and candidate-order digest are passed to every selected
  Master condition. Web search and candidate-external URLs are disabled.

Outputs:
  logs/master-paired-audit/<session-id>/
    candidate-pools/seed_<N>.json
    results/seed_<N>_<condition>.json
    results/seed_<N>_<condition>.metadata.json
    results/seed_<N>_<condition>.codex.usage.json
    runs.tsv
USAGE
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app_dir="$(cd "$script_dir/.." && pwd)"
python_bin="${NEWSBOT_MASTER_AUDIT_PYTHON_BIN:-$app_dir/.venv/bin/python}"
codex_bin="${NEWSBOT_CODEX_BIN:-codex}"
worker="$app_dir/scripts/run_master_evaluation.py"

period=""
source_runs=()
seeds=(101 202 303)
custom_seeds=0
only_condition=""
max_items=30
codex_timeout=5400
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
    --source-run)
      [[ $# -ge 2 ]] || {
        echo "--source-run requires a value." >&2
        exit 2
      }
      source_runs+=("$2")
      shift 2
      ;;
    --seed)
      [[ $# -ge 2 ]] || {
        echo "--seed requires a value." >&2
        exit 2
      }
      if [[ "$custom_seeds" -eq 0 ]]; then
        seeds=()
        custom_seeds=1
      fi
      seeds+=("$2")
      shift 2
      ;;
    --only)
      [[ $# -ge 2 ]] || {
        echo "--only requires a value." >&2
        exit 2
      }
      only_condition="$2"
      shift 2
      ;;
    --max-items)
      [[ $# -ge 2 ]] || {
        echo "--max-items requires a value." >&2
        exit 2
      }
      max_items="$2"
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
  echo "--period is required." >&2
  usage >&2
  exit 2
fi
if [[ "${#source_runs[@]}" -ne 2 ]]; then
  echo "Specify exactly two --source-run values." >&2
  exit 2
fi
if ! [[ "$max_items" =~ ^[1-9][0-9]*$ ]] || [[ "$max_items" -gt 30 ]]; then
  echo "--max-items must be an integer from 1 to 30: $max_items" >&2
  exit 2
fi
if ! [[ "$codex_timeout" =~ ^[1-9][0-9]*$ ]]; then
  echo "--codex-timeout must be a positive integer: $codex_timeout" >&2
  exit 2
fi
for seed in "${seeds[@]}"; do
  if ! [[ "$seed" =~ ^-?[0-9]+$ ]]; then
    echo "--seed must be an integer: $seed" >&2
    exit 2
  fi
done

condition_names=(
  terra-medium
  terra-high
  sol-medium
  sol-high
  gpt55-high
  luna-high
)
master_models=(
  gpt-5.6-terra
  gpt-5.6-terra
  gpt-5.6-sol
  gpt-5.6-sol
  gpt-5.5
  gpt-5.6-luna
)
reasoning_efforts=(
  medium
  high
  medium
  high
  high
  high
)

condition_selected() {
  local index="$1"
  [[ -z "$only_condition" ]] \
    || [[ "${condition_names[$index]}" == "$only_condition" ]]
}

if [[ -n "$only_condition" ]]; then
  valid_condition=0
  for name in "${condition_names[@]}"; do
    if [[ "$name" == "$only_condition" ]]; then
      valid_condition=1
      break
    fi
  done
  if [[ "$valid_condition" -eq 0 ]]; then
    echo "Unknown Master condition: $only_condition" >&2
    echo "Expected one of: ${condition_names[*]}" >&2
    exit 2
  fi
fi

selected_condition_count="${#condition_names[@]}"
if [[ -n "$only_condition" ]]; then
  selected_condition_count=1
fi
total_runs=$((selected_condition_count * ${#seeds[@]}))

echo "Paired Master audit plan"
echo "  repository: $app_dir"
echo "  period: $period"
echo "  source run 1: ${source_runs[0]}"
echo "  source run 2: ${source_runs[1]}"
echo "  paired candidate-order seeds: ${seeds[*]}"
echo "  maximum selections: $max_items"
echo "  paid Master turns: $total_runs"
echo "  Discord submission: disabled"
for index in "${!condition_names[@]}"; do
  condition_selected "$index" || continue
  echo "  - ${condition_names[$index]}: ${master_models[$index]}/${reasoning_efforts[$index]}"
done

if [[ "$execute_runs" -eq 0 ]]; then
  echo
  echo "Plan only. Add --execute to prepare pools and start paid Codex turns."
  exit 0
fi

if [[ ! -x "$python_bin" ]]; then
  echo "Python executable not found: $python_bin" >&2
  exit 1
fi
if [[ ! -f "$worker" ]]; then
  echo "Master evaluation worker not found: $worker" >&2
  exit 1
fi
if ! command -v "$codex_bin" >/dev/null 2>&1 \
  && [[ ! -x "$codex_bin" ]]; then
  echo "Codex executable not found: $codex_bin" >&2
  exit 1
fi

session_id="$(date +%Y%m%d_%H%M%S_%6N)"
audit_root="$app_dir/logs/master-paired-audit"
session_dir="$audit_root/$session_id"
pool_dir="$session_dir/candidate-pools"
result_dir="$session_dir/results"
manifest_path="$session_dir/runs.tsv"
mkdir -p "$pool_dir" "$result_dir" || exit 1

exec 9>"$audit_root/audit.lock" || exit 1
if ! flock -n 9; then
  echo "Another paired Master audit is already running." >&2
  exit 1
fi

printf 'sequence\tseed\tcondition\tmodel\treasoning\tstatus\texit_code\tcandidate_digest\tselected_items\toutput\tconsole_log\n' \
  >"$manifest_path" || exit 1

echo
echo "Preflight"
"$python_bin" -m py_compile \
  "$app_dir/scripts/generate_payload_openai.py" \
  "$worker" || exit 1
"$codex_bin" --version || exit 1
echo "  session: $session_id"
echo "  manifest: $manifest_path"

for seed in "${seeds[@]}"; do
  pool_path="$pool_dir/seed_${seed}.json"
  "$python_bin" "$worker" prepare-pool \
    --source-run "${source_runs[0]}" \
    --source-run "${source_runs[1]}" \
    --period "$period" \
    --seed "$seed" \
    --output "$pool_path" || exit 1
done

failures=0
sequence=0
for seed in "${seeds[@]}"; do
  pool_path="$pool_dir/seed_${seed}.json"
  candidate_digest="$(
    "$python_bin" -c \
      'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["candidate_order_digest"])' \
      "$pool_path"
  )"

  for index in "${!condition_names[@]}"; do
    condition_selected "$index" || continue
    sequence=$((sequence + 1))
    name="${condition_names[$index]}"
    output_path="$result_dir/seed_${seed}_${name}.json"
    console_log="$result_dir/seed_${seed}_${name}.console.log"

    echo
    echo "[$sequence/$total_runs] seed=$seed condition=$name"
    echo "  candidate digest: $candidate_digest"
    echo "  output: $output_path"

    (
      cd "$app_dir" || exit 1
      "$python_bin" "$worker" run-master \
        --candidate-pool "$pool_path" \
        --master-model "${master_models[$index]}" \
        --reasoning-effort "${reasoning_efforts[$index]}" \
        --max-items "$max_items" \
        --codex-bin "$codex_bin" \
        --codex-timeout "$codex_timeout" \
        --output "$output_path"
    ) 2>&1 | tee "$console_log"
    exit_code=${PIPESTATUS[0]}

    metadata_path="${output_path%.json}.metadata.json"
    selected_items=""
    if [[ "$exit_code" -eq 0 && -f "$metadata_path" ]]; then
      status=success
      selected_items="$(
        "$python_bin" -c \
          'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["selected_item_count"])' \
          "$metadata_path"
      )"
    else
      status=failed
      failures=$((failures + 1))
    fi

    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$sequence" \
      "$seed" \
      "$name" \
      "${master_models[$index]}" \
      "${reasoning_efforts[$index]}" \
      "$status" \
      "$exit_code" \
      "$candidate_digest" \
      "$selected_items" \
      "$output_path" \
      "$console_log" \
      >>"$manifest_path"

    echo "  result: $status (exit_code=$exit_code)"
    if [[ "$exit_code" -ne 0 && "$stop_on_error" -eq 1 ]]; then
      echo "Stopping after first failure. See: $manifest_path" >&2
      exit "$exit_code"
    fi
  done
done

echo
echo "Paired Master audit finished"
echo "  successful: $((total_runs - failures))"
echo "  failed: $failures"
echo "  session: $session_dir"
echo "  manifest: $manifest_path"

if [[ "$failures" -gt 0 ]]; then
  exit 1
fi

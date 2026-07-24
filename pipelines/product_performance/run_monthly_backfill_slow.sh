#!/usr/bin/env bash
set -u

PROJECT_DIR="${PROJECT_DIR:-/data/bi_scripts/amazon-growth-attribution}"
PYTHON_BIN="${PYTHON_BIN:-/data/venvs/bi_venv/bin/python}"
JOB_FILE="$PROJECT_DIR/pipelines/product_performance/ods_lx_product_performance_monthly_msku_v3.py"

START_MONTH="${START_MONTH:-2025-01}"
END_MONTH="${END_MONTH:-2026-07}"
STORES="${STORES:-MT-US,SY-US,JQ-US,RKZ-US,CY-US}"
RESTART_WAIT_MIN="${RESTART_WAIT_MIN_SECONDS:-300}"
RESTART_WAIT_MAX="${RESTART_WAIT_MAX_SECONDS:-600}"

export MAX_RETRIES="${MAX_RETRIES:-30}"
export REQUEST_INTERVAL_SECONDS="${REQUEST_INTERVAL_SECONDS:-15}"
export LIMIT_WAIT_BASE_SECONDS="${LIMIT_WAIT_BASE_SECONDS:-20}"
export LIMIT_WAIT_CAP_SECONDS="${LIMIT_WAIT_CAP_SECONDS:-300}"
export ERROR_WAIT_BASE_SECONDS="${ERROR_WAIT_BASE_SECONDS:-10}"
export ERROR_WAIT_CAP_SECONDS="${ERROR_WAIT_CAP_SECONDS:-180}"
export RETRY_JITTER_MIN_SECONDS="${RETRY_JITTER_MIN_SECONDS:-5}"
export RETRY_JITTER_MAX_SECONDS="${RETRY_JITTER_MAX_SECONDS:-20}"

cd "$PROJECT_DIR" || exit 1

for required in LINGXING_APP_ID LINGXING_APP_SECRET LINGXING_DB_PASSWORD; do
  if [[ -z "${!required:-}" ]]; then
    echo "[$(date '+%F %T')] 缺少环境变量：$required" >&2
    exit 2
  fi
done

while true; do
  echo "[$(date '+%F %T')] 启动回补：$START_MONTH ~ $END_MONTH；店铺=$STORES"
  echo "[$(date '+%F %T')] 请求间隔=${REQUEST_INTERVAL_SECONDS}s；单请求最大重试=${MAX_RETRIES}"

  "$PYTHON_BIN" "$JOB_FILE" \
    --start-month "$START_MONTH" \
    --end-month "$END_MONTH" \
    --stores "$STORES"

  rc=$?
  if [[ $rc -eq 0 ]]; then
    echo "[$(date '+%F %T')] 全部任务完成，后台进程退出。"
    exit 0
  fi

  if (( RESTART_WAIT_MAX > RESTART_WAIT_MIN )); then
    wait_seconds=$((RESTART_WAIT_MIN + RANDOM % (RESTART_WAIT_MAX - RESTART_WAIT_MIN + 1)))
  else
    wait_seconds=$RESTART_WAIT_MIN
  fi

  echo "[$(date '+%F %T')] 任务异常退出 rc=$rc；${wait_seconds}s 后自动重启。"
  sleep "$wait_seconds"
done

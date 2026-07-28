#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

bash "$SCRIPT_DIR/run_attribution_pipeline_7usd.sh" "$@"
bash "$SCRIPT_DIR/ensure_product_audit_compat.sh"

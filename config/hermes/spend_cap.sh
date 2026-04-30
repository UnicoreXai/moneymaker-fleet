#!/bin/bash
# /opt/mm-agent/spend_cap.sh
# Hard pre-flight cap on Hermes runs. Polls OpenRouter usage API; aborts
# if today's per-node spend ≥ $0.50 OR the node hasn't been initialized.
#
# Wraps the actual hermes invocation. Call as:
#   spend_cap.sh hermes run --prompt-file /etc/mm-agent/system_prompt.md ...
set -euo pipefail

KEY_FILE="/etc/mm-agent/openrouter.key"
NODE_FILE="/etc/mm-agent/node.json"
LOG_DIR="/var/log/mm-agent"
DAILY_CAP_USD="10.00"  # 2026-04-28 — fleet-wide $10/day gate. OpenRouter /credits returns ACCOUNT-WIDE total_usage (all 25 workers share key), so this is read as a shared counter, not per-node.

mkdir -p "$LOG_DIR"

if [[ ! -f "$KEY_FILE" ]]; then
  echo "[spend_cap] no OpenRouter key at $KEY_FILE" >&2
  exit 2
fi
if [[ ! -f "$NODE_FILE" ]]; then
  echo "[spend_cap] not initialized — $NODE_FILE missing" >&2
  exit 2
fi

API_KEY="$(cat "$KEY_FILE")"
NODE_LABEL="$(jq -r '.label' "$NODE_FILE")"

# Pull today's usage from OpenRouter
TODAY="$(date -u +%Y-%m-%d)"
USAGE_JSON="$(curl -sS -H "Authorization: Bearer $API_KEY" \
  "https://openrouter.ai/api/v1/credits" || echo '{}')"

# OpenRouter returns { data: { total_credits, total_usage } }
TOTAL_USAGE="$(echo "$USAGE_JSON" | jq -r '.data.total_usage // 0')"

# Track per-day spend locally (OpenRouter doesn't split by day in /credits)
LAST_FILE="$LOG_DIR/last_usage_${TODAY}.txt"
START_FILE="$LAST_FILE.start"
# Seed start-of-day baseline once per UTC day
if [[ ! -f "$START_FILE" ]]; then
  echo "$TOTAL_USAGE" > "$START_FILE"
fi
START_USAGE="$(cat "$START_FILE")"
TODAY_USAGE="$(echo "$TOTAL_USAGE - $START_USAGE" | bc -l)"
# Persist running tally (cycle.sh + dashboard read this file)
printf '%s\n' "$TODAY_USAGE" > "$LAST_FILE"

# Compare to cap
OVER="$(echo "$TODAY_USAGE >= $DAILY_CAP_USD" | bc -l)"
if [[ "$OVER" == "1" ]]; then
  echo "{\"ts\":\"$(date -uIs)\",\"track\":\"meta\",\"action\":\"spend_cap_hit\",\"node\":\"$NODE_LABEL\",\"today_usd\":$TODAY_USAGE,\"cap_usd\":$DAILY_CAP_USD}" >> "$LOG_DIR/actions.log"
  echo "[spend_cap] DAILY CAP HIT: \$$TODAY_USAGE >= \$$DAILY_CAP_USD on $NODE_LABEL" >&2
  # 2026-04-28 — emit a forum heartbeat so silent cap hits are visible to queen synthesis
  REPORT_DIR="/var/lib/mm-agent/reports"
  mkdir -p "$REPORT_DIR"
  HB_TS="$(date -uIs)"
  HB_FILE="$REPORT_DIR/$(date -u +%Y%m%dT%H%M%SZ).jsonl"
  printf '{"ts":"%s","from":"[%s - capped]","topic":"meta","msg":"spend_cap_hit today_usd=%s cap=%s — cycle skipped"}\n' \
    "$HB_TS" "$NODE_LABEL" "$TODAY_USAGE" "$DAILY_CAP_USD" >> "$HB_FILE"

  # 2026-04-28: also POST to live forum API (best-effort, never abort).
  FORUM_SECRET_FILE="/etc/mm-agent/forum_secret"
  if [[ -f "$FORUM_SECRET_FILE" ]] && command -v jq >/dev/null 2>&1; then
    NODE_NUM_CAP="$(echo "$NODE_LABEL" | grep -oE '[0-9]+$' || echo 0)"
    NODE_ID_CAP="h-${NODE_NUM_CAP}"
    CAP_BODY="$(jq -nc \
      --arg node "$NODE_ID_CAP" \
      --arg model "capped" \
      --arg topic "alert" \
      --arg msg "spend_cap_hit today_usd=${TODAY_USAGE} cap=${DAILY_CAP_USD}" \
      '{node:$node, model:$model, topic:$topic, msg:$msg}')"
    (curl -sS --max-time 10 \
       -H "X-Forum-Secret: $(cat "$FORUM_SECRET_FILE")" \
       -H "Content-Type: application/json" \
       -X POST -d "$CAP_BODY" \
       "https://dashboard.example.com/api/forum" >/dev/null 2>>"$LOG_DIR/forum_post.log" || true) &
  fi

  exit 1
fi

# Pass through to hermes
exec "$@"

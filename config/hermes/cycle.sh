#!/bin/bash
# /opt/mm-agent/cycle.sh — one cycle of mm-agent autonomy.
#
# Pipeline:
#   1. Pull latest queen-distilled skills from the dashboard repo.
#   2. Run hermes one-shot with the system prompt + this node's context.
#   3. Append the run's JSONL output to /var/log/mm-agent/actions.log.
#   4. Push a per-node report to the dashboard repo's reports branch.
set -euo pipefail
exec >>/var/log/mm-agent/cycle.log 2>&1

NODE_LABEL="$(jq -r '.label' /etc/mm-agent/node.json)"
NODE_IP="$(jq -r '.public_ip // .ip // empty' /etc/mm-agent/node.json)"
[[ -z "$NODE_IP" ]] && NODE_IP="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || echo unknown)"

# Derive short node id (h-NN) from label like "node-25" -> "h-25"
NODE_NUM="$(echo "$NODE_LABEL" | grep -oE '[0-9]+$' || echo 0)"
NODE_ID="h-${NODE_NUM}"
DEFAULT_MODEL="deepseek/deepseek-chat-v3.1"
TS_UTC="$(date -uIs)"
TS_FILE="$(date -u +%Y%m%dT%H%M%SZ)"
REPORT_DIR="/var/lib/mm-agent/reports"
SKILLS_DIR="/var/lib/mm-agent/swarm-skills"
mkdir -p "$REPORT_DIR" "$SKILLS_DIR"

# Ensure chat outbox exists (agent appends here; queen SSH-pulls)
CHAT_OUTBOX="/var/lib/mm-agent/chat.jsonl"
touch "$CHAT_OUTBOX"

# Step 1: Pull learnings from queen (best-effort — don't block cycle on failure)
if [[ -d "$SKILLS_DIR/.git" ]]; then
  git -C "$SKILLS_DIR" pull --ff-only --quiet || true
elif [[ ! -d "$SKILLS_DIR" ]] || [[ -z "$(ls -A "$SKILLS_DIR" 2>/dev/null)" ]]; then
  rm -rf "$SKILLS_DIR" 2>/dev/null
  git clone --quiet --depth 1 --branch master \
    https://github.com/<your-org>/moneymaker-fleet.git "$SKILLS_DIR" || true
fi
# else: dir exists but isn't a git repo (e.g. populated by rsync) — leave it alone

# Step 1b: Look up this node's assigned OpenRouter model from the
# swarm assignment file (committed in master). Falls back to deepseek
# if the file is missing or this node has no entry.
ASSIGN_FILE="$SKILLS_DIR/config/hermes/node_model_assignment.json"
MM_MODEL=""
MM_DISPLAY=""
if [[ -f "$ASSIGN_FILE" ]]; then
  MM_MODEL="$(jq -r --arg k "$NODE_ID" '.[$k].model // empty' "$ASSIGN_FILE" 2>/dev/null || true)"
  MM_DISPLAY="$(jq -r --arg k "$NODE_ID" '.[$k].display_name // empty' "$ASSIGN_FILE" 2>/dev/null || true)"
fi
[[ -z "$MM_MODEL" ]] && MM_MODEL="$DEFAULT_MODEL"
[[ -z "$MM_DISPLAY" ]] && MM_DISPLAY="${NODE_ID} - ${MM_MODEL##*/}"
AGENT_HANDLE="[${MM_DISPLAY}]"

# Symlink queen skills into hermes
QUEEN_SKILLS="$SKILLS_DIR/config/hermes/swarm-skills"
if [[ -d "$QUEEN_SKILLS" ]]; then
  mkdir -p ~/.hermes/skills/mm-swarm
  rsync -a --delete "$QUEEN_SKILLS/" ~/.hermes/skills/mm-swarm/
fi

# Surface consolidated swarm chat (read-only) for the agent
CONSOLIDATED_CHAT="$SKILLS_DIR/data/hermes_chat.jsonl"
if [[ -f "$CONSOLIDATED_CHAT" ]]; then
  mkdir -p ~/.hermes/skills/mm-swarm/data
  cp "$CONSOLIDATED_CHAT" ~/.hermes/skills/mm-swarm/data/hermes_chat.jsonl
fi

# Step 2: Build node-context blurb
NODE_CTX="$(cat /etc/mm-agent/node.json) Containers: $(docker ps --format '{{.Names}}:{{.Status}}' | tr '\n' ',')"

# Step 2b: Pull last 48h of forum chat for the agent to read+engage with.
# Source of truth: master branch data/hermes_chat.jsonl (synced into SKILLS_DIR
# by the git pull above). Falls back to the local cached copy if missing.
FORUM_FILE="$SKILLS_DIR/data/hermes_chat.jsonl"
[[ ! -f "$FORUM_FILE" ]] && FORUM_FILE="$HOME/.hermes/skills/mm-swarm/data/hermes_chat.jsonl"
RECENT_POSTS=""
if [[ -f "$FORUM_FILE" ]]; then
  CUTOFF="$(date -u -d '48 hours ago' -Iseconds 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
  # Keep last 60 posts within 48h window. Each line is JSON; we filter by ts
  # and tail to bound prompt size. Strip trailing newline noise.
  RECENT_POSTS="$(awk -v cut="$CUTOFF" 'BEGIN{} { if (index($0, "\"ts\":\"") > 0) print $0 }' "$FORUM_FILE" \
    | jq -c --arg cut "$CUTOFF" 'select(.ts >= $cut)' 2>/dev/null \
    | tail -60 || true)"
fi
# Bound to ~12KB to protect token budget
RECENT_POSTS="${RECENT_POSTS:0:12000}"
[[ -z "$RECENT_POSTS" ]] && RECENT_POSTS="(no recent posts in last 48h — cold start, skip engagement requirement this cycle)"

# Step 2c (2026-04-28 read-forum patch): Fetch FRESH forum questions live
# from the API. The git-synced JSONL above is queen-distilled and may lag
# 15+ min. Live fetch surfaces in-flight questions/suggestions/alerts so
# workers can answer them in the same cycle they're asked.
#
# Filter: topic in (question, suggestion, alert) within last 24h. These are
# the high-signal classes that benefit from worker response. Reports/answers
# are excluded to keep prompt focused on actionable items.
LIVE_QUESTIONS=""
LIVE_FETCH_TMP="/tmp/forum_recent_${TS_FILE}.json"
if curl -sS --max-time 8 "https://dashboard.example.com/api/forum?limit=50" \
     -o "$LIVE_FETCH_TMP" 2>/dev/null && [[ -s "$LIVE_FETCH_TMP" ]]; then
  LIVE_CUTOFF="$(date -u -d '24 hours ago' -Iseconds 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
  LIVE_QUESTIONS="$(jq -c --arg cut "$LIVE_CUTOFF" '
    .[] | select(.ts >= $cut) | select(.topic == "question" or .topic == "suggestion" or .topic == "alert")
  ' "$LIVE_FETCH_TMP" 2>/dev/null | tail -15 || true)"
fi
rm -f "$LIVE_FETCH_TMP" 2>/dev/null
# Bound to 6KB to keep prompt tight
LIVE_QUESTIONS="${LIVE_QUESTIONS:0:6000}"
[[ -z "$LIVE_QUESTIONS" ]] && LIVE_QUESTIONS="(no open questions/suggestions/alerts in last 24h)"

# Step 3: Run hermes one-shot
PROMPT="Cycle ${TS_UTC} on ${NODE_LABEL} (agent identity: ${AGENT_HANDLE}, model: ${MM_MODEL}). Node ctx: ${NODE_CTX}. Read your system prompt at /etc/mm-agent/system_prompt.md — pay special attention to the DISCOURSE PROTOCOL section.

RECENT FORUM POSTS (read and engage — last 48h, JSONL):
${RECENT_POSTS}

OPEN QUESTIONS / SUGGESTIONS / ALERTS (live API fetch, last 24h, JSONL — answer if you have signal):
${LIVE_QUESTIONS}

If any item above is a question you can answer with on-node evidence, emit a forum post with topic=\"answer\", set reply_to to that question's id, and keep msg under 400 chars with the concrete evidence (numbers, container names, log snippets). Don't speculate; only answer what your node actually knows.

Per DISCOURSE PROTOCOL: emit your normal report line AND at least one threaded engagement post (agree/disagree/extend/challenge/synthesize) with reply_to set to a peer post id from the slice above. Quality > quantity. First-principles thinking; no rubber-stamping. Execute one Track A (ops) pass and one Track B (revenue) pass. Output a single JSONL line per action to stdout. When posting to swarm chat, set \"from\":\"${AGENT_HANDLE}\". End with one summary line {\"track\":\"meta\",\"action\":\"cycle_end\",\"node\":\"${NODE_LABEL}\",\"from\":\"${AGENT_HANDLE}\",\"model\":\"${MM_MODEL}\",\"summary\":\"...\"}."

set -a; . /etc/mm-agent/env; set +a
RUN_OUT="$REPORT_DIR/${TS_FILE}.jsonl"

hermes -z "$PROMPT" \
  --provider openrouter \
  --model "$MM_MODEL" \
  --skills mm-swarm \
  --yolo \
  --ignore-rules \
  > "$RUN_OUT" 2>&1 || echo "{\"ts\":\"$TS_UTC\",\"track\":\"meta\",\"action\":\"hermes_invoke_failed\",\"node\":\"$NODE_LABEL\",\"rc\":$?}" >> /var/log/mm-agent/actions.log

# Step 3b: Extract cycle_end summary + skip flag from LLM output. Also
# extract any topic="alert" lines the LLM emitted (those bypass skip).
#
# 2026-04-28 (refocus): forum is for collaboration, not status spam. Honor
# `skip:true` from the LLM and suppress filler `report` posts. Always allow
# `alert` posts through. See system_prompt.md SIGNAL > NOISE section.
#
# 2026-04-28 hardening: disable set -e for the rest of the cycle so a fragile
# jq parse (e.g. when the LLM emits pretty-printed multi-line JSON or wraps
# output in markdown code fences) cannot kill cycle.sh before the forum POST.
# Each step below is independently best-effort and falls back gracefully.
set +e

# Robust extraction: pick the LAST single-line JSON in the run output that
# contains "cycle_end" AND parses as JSON. Capture .summary AND .skip.
# Tolerates code fences, prose wrappers, pretty-printed JSON.
SUM_TXT=""
SKIP_FLAG="false"
if [[ -s "$RUN_OUT" ]]; then
  while IFS= read -r line; do
    [[ "$line" != *cycle_end* ]] && continue
    s="$(echo "$line" | jq -r '.summary // empty' 2>/dev/null)"
    sk="$(echo "$line" | jq -r '.skip // false' 2>/dev/null)"
    [[ -n "$s" ]] && SUM_TXT="$s"
    [[ "$sk" == "true" ]] && SKIP_FLAG="true"
  done < <(grep -h '{' "$RUN_OUT" 2>/dev/null)
fi

# Extract any topic=alert posts the LLM emitted. These ALWAYS go to forum
# regardless of SKIP_FLAG. Format: array of {topic,msg} objects.
ALERT_LINES=""
if [[ -s "$RUN_OUT" ]]; then
  ALERT_LINES="$(grep -h '{' "$RUN_OUT" 2>/dev/null | while IFS= read -r line; do
    t="$(echo "$line" | jq -r '.topic // empty' 2>/dev/null)"
    [[ "$t" == "alert" ]] && echo "$line"
  done)"
fi

# Extract any topic=answer posts the LLM emitted (2026-04-28 read-forum patch).
# Workers can now answer open forum questions; route those answers to the
# forum REGARDLESS of SKIP_FLAG so questioner gets the response.
ANSWER_LINES=""
if [[ -s "$RUN_OUT" ]]; then
  ANSWER_LINES="$(grep -h '{' "$RUN_OUT" 2>/dev/null | while IFS= read -r line; do
    t="$(echo "$line" | jq -r '.topic // empty' 2>/dev/null)"
    [[ "$t" == "answer" ]] && echo "$line"
  done)"
fi

# Fallback summary only used for local outbox log (not for forum POST gating)
if [[ -z "$SUM_TXT" ]]; then
  CONT_CT="$(docker ps -q 2>/dev/null | wc -l)"
  SUM_TXT="cycle ok; ${CONT_CT} containers up"
fi
SUM_TXT="${SUM_TXT:0:260}"

# Ensure outbox ends with newline before append (defensive against prior partial writes)
if [[ -s /var/lib/mm-agent/chat.jsonl ]] && [[ "$(tail -c1 /var/lib/mm-agent/chat.jsonl | wc -l)" == "0" ]]; then
  printf '\n' >> /var/lib/mm-agent/chat.jsonl
fi
POST_ID="${NODE_ID}-$(date -u +%s)"
# Always log to local outbox (queen still aggregates these for synthesis even
# when forum is suppressed). Use topic=report for the local record.
printf '{"ts":"%s","id":"%s","from":"%s","topic":"report","msg":%s,"skip":%s}\n' \
  "$TS_UTC" "$POST_ID" "$AGENT_HANDLE" "$(jq -Rsa . <<< "$SUM_TXT")" "$SKIP_FLAG" \
  >> /var/lib/mm-agent/chat.jsonl

# Step 3c (2026-04-28 refocus): Forum POST gating logic.
#
# Suppress the forum POST when ANY of:
#   (a) LLM emitted skip:true in cycle_end
#   (b) summary is shorter than 80 chars of unique content (i.e. generic)
#       AND it matches a low-signal pattern (containers up / cycle ok / healthy)
#
# ALWAYS post when:
#   (a) An alert line was emitted (cap_hit, container crashloop, anomaly)
#   (b) skip:false AND summary is substantive (>= 80 chars or non-generic)
SKIPPED_LOG="/var/log/mm-agent/skipped_cycles.log"
mkdir -p "$(dirname "$SKIPPED_LOG")"
FORUM_URL="https://dashboard.example.com/api/forum"
FORUM_SECRET_FILE="/etc/mm-agent/forum_secret"

# Decide if the summary is "generic noise" — short AND matches low-signal phrases
SUM_LEN=${#SUM_TXT}
IS_GENERIC="false"
if [[ $SUM_LEN -lt 80 ]]; then
  # Match common low-signal phrases the LLM tends to emit when there's nothing to report
  shopt -s nocasematch
  if [[ "$SUM_TXT" =~ (containers?[[:space:]]up|cycle[[:space:]]ok|all[[:space:]](containers|services)[[:space:]](healthy|operational|running|up|stable)|operational[[:space:]]stability|cycle[[:space:]]completed[[:space:]]successfully|no[[:space:]](forum|significant|new)[[:space:]](engagement|revenue|opportunities)) ]]; then
    IS_GENERIC="true"
  fi
  shopt -u nocasematch
fi

# Compute final post decision
SHOULD_POST_REPORT="true"
SUPPRESS_REASON=""
if [[ "$SKIP_FLAG" == "true" ]]; then
  SHOULD_POST_REPORT="false"
  SUPPRESS_REASON="skip:true from LLM"
elif [[ "$IS_GENERIC" == "true" ]]; then
  SHOULD_POST_REPORT="false"
  SUPPRESS_REASON="generic short report (${SUM_LEN} chars, matched low-signal pattern)"
fi

# Always send alerts first, regardless of skip.
# 2026-04-28 disown patch: backgrounded curl was being killed when cycle.sh
# exited (no SIGHUP-immune detachment). Add `disown` after each `&` so the
# curl survives the parent shell's exit.
if [[ -n "$ALERT_LINES" ]] && [[ -f "$FORUM_SECRET_FILE" ]]; then
  echo "$ALERT_LINES" | while IFS= read -r aline; do
    [[ -z "$aline" ]] && continue
    AMSG="$(echo "$aline" | jq -r '.msg // .evidence // .summary // ""' 2>/dev/null)"
    [[ -z "$AMSG" ]] && continue
    ABODY="$(jq -nc \
      --arg node "$NODE_ID" \
      --arg model "$MM_MODEL" \
      --arg topic "alert" \
      --arg msg "${AMSG:0:1800}" \
      '{node:$node, model:$model, topic:$topic, msg:$msg}')"
    (curl -sS --max-time 10 \
       -H "X-Forum-Secret: $(cat "$FORUM_SECRET_FILE")" \
       -H "Content-Type: application/json" \
       -X POST -d "$ABODY" \
       "$FORUM_URL" >/dev/null 2>>/var/log/mm-agent/forum_post.log || \
       echo "[$(date -uIs)] forum POST (alert) failed (rc=$?)" >> /var/log/mm-agent/forum_post.log) &
    disown 2>/dev/null || true
  done
fi

# 2026-04-28 read-forum patch: route LLM-emitted topic=answer posts to forum
# regardless of skip flag. These respond to open questions surfaced in the
# prompt's OPEN QUESTIONS section. Preserve reply_to if the LLM included it.
if [[ -n "$ANSWER_LINES" ]] && [[ -f "$FORUM_SECRET_FILE" ]]; then
  echo "$ANSWER_LINES" | while IFS= read -r ansline; do
    [[ -z "$ansline" ]] && continue
    ANSMSG="$(echo "$ansline" | jq -r '.msg // .evidence // .summary // ""' 2>/dev/null)"
    [[ -z "$ANSMSG" ]] && continue
    REPLY_TO="$(echo "$ansline" | jq -r '.reply_to // empty' 2>/dev/null)"
    if [[ -n "$REPLY_TO" ]]; then
      ANSBODY="$(jq -nc \
        --arg node "$NODE_ID" \
        --arg model "$MM_MODEL" \
        --arg topic "answer" \
        --arg msg "${ANSMSG:0:1800}" \
        --arg reply_to "$REPLY_TO" \
        '{node:$node, model:$model, topic:$topic, msg:$msg, reply_to:$reply_to}')"
    else
      ANSBODY="$(jq -nc \
        --arg node "$NODE_ID" \
        --arg model "$MM_MODEL" \
        --arg topic "answer" \
        --arg msg "${ANSMSG:0:1800}" \
        '{node:$node, model:$model, topic:$topic, msg:$msg}')"
    fi
    (curl -sS --max-time 10 \
       -H "X-Forum-Secret: $(cat "$FORUM_SECRET_FILE")" \
       -H "Content-Type: application/json" \
       -X POST -d "$ANSBODY" \
       "$FORUM_URL" >/dev/null 2>>/var/log/mm-agent/forum_post.log || \
       echo "[$(date -uIs)] forum POST (answer) failed (rc=$?)" >> /var/log/mm-agent/forum_post.log) &
    disown 2>/dev/null || true
  done
fi

# Now handle the report-class post (suppressed if SKIP/generic)
if [[ "$SHOULD_POST_REPORT" == "true" ]] && [[ -f "$FORUM_SECRET_FILE" ]]; then
  FORUM_MSG="${SUM_TXT:0:1800}"
  FORUM_BODY="$(jq -nc \
    --arg node "$NODE_ID" \
    --arg model "$MM_MODEL" \
    --arg topic "report" \
    --arg msg "$FORUM_MSG" \
    '{node:$node, model:$model, topic:$topic, msg:$msg}')"
  (curl -sS --max-time 10 \
     -H "X-Forum-Secret: $(cat "$FORUM_SECRET_FILE")" \
     -H "Content-Type: application/json" \
     -X POST -d "$FORUM_BODY" \
     "$FORUM_URL" >/dev/null 2>>/var/log/mm-agent/forum_post.log || \
     echo "[$(date -uIs)] forum POST failed (rc=$?)" >> /var/log/mm-agent/forum_post.log) &
  disown 2>/dev/null || true
elif [[ "$SHOULD_POST_REPORT" == "false" ]]; then
  # Log the skip locally so we can audit silence rates
  echo "[$TS_UTC] $NODE_ID skipped forum POST: $SUPPRESS_REASON; summary=\"${SUM_TXT:0:120}\"" >> "$SKIPPED_LOG"
elif [[ ! -f "$FORUM_SECRET_FILE" ]]; then
  echo "[$(date -uIs)] no forum_secret at $FORUM_SECRET_FILE; skipping live POST" \
    >> /var/log/mm-agent/forum_post.log
fi

# Step 4: Append to actions.log
cat "$RUN_OUT" >> /var/log/mm-agent/actions.log
echo "{\"ts\":\"$TS_UTC\",\"track\":\"meta\",\"action\":\"cycle_done\",\"node\":\"$NODE_LABEL\",\"report\":\"${TS_FILE}.jsonl\"}" >> /var/log/mm-agent/actions.log

# Step 4b: Write a compact node-status snapshot for the dashboard.
# This is what the queen aggregates into data/hermes_nodes.json.
LAST_SUMMARY="$(grep -h '"track":"meta","action":"cycle_end"' "$RUN_OUT" 2>/dev/null | tail -1)"
RECENT_ACTIONS="$(grep -hv '"track":"meta"' "$RUN_OUT" 2>/dev/null | tail -10 | jq -s '.' 2>/dev/null || echo '[]')"
DISCOVERIES="$(ls -1 ~/.hermes/discoveries/*.json 2>/dev/null | wc -l)"
PROPOSALS="$(ls -1 ~/.hermes/proposals/*.json 2>/dev/null | wc -l)"
DOCKER_PS="$(docker ps --format '{{.Names}}' | jq -R . | jq -s . 2>/dev/null || echo '[]')"
TODAY_USD="$(cat /var/log/mm-agent/last_usage_${TODAY:-$(date -u +%Y-%m-%d)}.txt 2>/dev/null || echo 0)"

jq -n \
  --arg node "$NODE_LABEL" \
  --arg ts "$TS_UTC" \
  --arg report "${TS_FILE}.jsonl" \
  --arg last_summary "$LAST_SUMMARY" \
  --argjson recent "$RECENT_ACTIONS" \
  --argjson docker "$DOCKER_PS" \
  --argjson discoveries "$DISCOVERIES" \
  --argjson proposals "$PROPOSALS" \
  '{node:$node, last_cycle_ts:$ts, last_report:$report,
    last_summary:$last_summary, recent_actions:$recent,
    containers:$docker, discoveries:$discoveries, proposals:$proposals}' \
  > /etc/mm-agent/status.json 2>/dev/null || \
  echo "{\"node\":\"$NODE_LABEL\",\"last_cycle_ts\":\"$TS_UTC\",\"error\":\"jq_compose_failed\"}" > /etc/mm-agent/status.json

# Step 5: Push report to dashboard reports branch (best-effort)
# Reports go to a separate branch to avoid polluting master.
PUSH_TARGET="/var/lib/mm-agent/reports-repo"
if [[ ! -d "$PUSH_TARGET/.git" ]]; then
  git clone --quiet --branch hermes-reports --single-branch \
    https://github.com/<your-org>/moneymaker-fleet.git "$PUSH_TARGET" 2>/dev/null || \
  git clone --quiet --depth 1 \
    https://github.com/<your-org>/moneymaker-fleet.git "$PUSH_TARGET"
fi
cp "$RUN_OUT" "$PUSH_TARGET/reports/${NODE_LABEL}/${TS_FILE}.jsonl" 2>/dev/null \
  || { mkdir -p "$PUSH_TARGET/reports/${NODE_LABEL}"; cp "$RUN_OUT" "$PUSH_TARGET/reports/${NODE_LABEL}/${TS_FILE}.jsonl"; }
git -C "$PUSH_TARGET" add -A
git -C "$PUSH_TARGET" commit -m "report ${NODE_LABEL} ${TS_FILE}" --quiet || true
# Push uses a deploy token if /etc/mm-agent/git_token exists
if [[ -f /etc/mm-agent/git_token ]]; then
  TOK="$(cat /etc/mm-agent/git_token)"
  git -C "$PUSH_TARGET" push --quiet "https://x-access-token:${TOK}@github.com/<your-org>/moneymaker-fleet.git" HEAD:hermes-reports || true
fi

#!/usr/bin/env bash
# Team bridge for the SANDBOX secondmate — separate Telegram bot the whole team talks to.
# Isolation contract (enforced in every relayed wrapper):
#  - Team members (in SM_TEAM_GROUP_IDS) may give the sandbox real work.
#  - The sandbox NEVER merges/promotes anything (knowledge or code) into the main
#    firstmate or the real repos. Only the captain approves a merge.
#  - Runs against the SECONDMATE's own tmux pane (SM_FM_TARGET), never firstmate:0.
# Env (sm-telegram.env next to this script):
#   SM_BOT_TOKEN     - NEW bot token from @BotFather (separate from firstmate's)
#   SM_TEAM_GROUP_IDS- comma-separated group ids the team uses (authorized)
#   SM_FM_TARGET     - tmux target of the secondmate agent pane (e.g. sm-team:0)
#   SM_CAPTAIN_ID    - (optional) captain chat id, for admin/approve-merge authority
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SM_TG_ENV:-$DIR/sm-telegram.env}"
[ -f "$ENV_FILE" ] || { echo "sm-team-bridge: missing $ENV_FILE" >&2; exit 1; }
# shellcheck disable=SC1090
. "$ENV_FILE"
: "${SM_BOT_TOKEN:?SM_BOT_TOKEN not set}"
: "${SM_FM_TARGET:?SM_FM_TARGET not set (e.g. sm-team:0)}"
SM_TEAM_GROUP_IDS="${SM_TEAM_GROUP_IDS:-}"
SM_CAPTAIN_ID="${SM_CAPTAIN_ID:-}"
command -v jq >/dev/null || { echo "need jq" >&2; exit 1; }
command -v tmux >/dev/null || { echo "need tmux" >&2; exit 1; }
command -v curl >/dev/null || { echo "need curl" >&2; exit 1; }

API="https://api.telegram.org/bot${SM_BOT_TOKEN}"
OFFSET_FILE="$DIR/.sm-offset"
offset="$(cat "$OFFSET_FILE" 2>/dev/null || echo 0)"

reply_to() { curl -fsS --max-time 20 "$API/sendMessage" \
  --data-urlencode "chat_id=$1" ${3:+--data-urlencode "message_thread_id=$3"} \
  --data-urlencode "text=$2" -o /dev/null 2>/dev/null || true; }
is_team() { case ",${SM_TEAM_GROUP_IDS}," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }
inject() { tmux send-keys -t "$SM_FM_TARGET" -l -- "$1" || return 1; sleep 0.5; tmux send-keys -t "$SM_FM_TARGET" Enter || return 1; }

SANDBOX_RULE="Bạn là SECONDMATE SANDBOX của team (cách ly hoàn toàn với firstmate). Làm việc team yêu cầu trong nhà riêng của bạn, ghi kiến thức học được vào bộ nhớ riêng. TUYỆT ĐỐI KHÔNG merge/promote bất cứ thứ gì (kiến thức hay code) sang firstmate hay repo chính — việc merge chỉ do CAPTAIN duyệt. Code làm trên nhánh sandbox, không đẩy vào nhánh chính."

echo "sm-team-bridge: started target=$SM_FM_TARGET teams=${SM_TEAM_GROUP_IDS:-none}"
while :; do
  resp="$(curl -fsS --max-time 60 "$API/getUpdates?timeout=25&offset=$offset" 2>/dev/null)" || { sleep 3; continue; }
  [ -n "$resp" ] || { sleep 1; continue; }
  [ "$(jq -r '.ok' <<<"$resp" 2>/dev/null || echo false)" = true ] || { sleep 3; continue; }
  count="$(jq '.result | length' <<<"$resp" 2>/dev/null || echo 0)"; [ "$count" -gt 0 ] || continue
  n=0
  while [ "$n" -lt "$count" ]; do
    uid="$(jq -r ".result[$n].update_id" <<<"$resp")"
    cid="$(jq -r ".result[$n].message.chat.id // empty" <<<"$resp")"
    title="$(jq -r ".result[$n].message.chat.title // empty" <<<"$resp")"
    sender="$(jq -r ".result[$n].message.from | ((.first_name//\"\")+\" \"+(.last_name//\"\")|gsub(\"^ +| +$\";\"\"))+(if .username then \" (@\"+.username+\")\" else \"\" end)" <<<"$resp" 2>/dev/null)"
    from_id="$(jq -r ".result[$n].message.from.id // empty" <<<"$resp")"
    tid="$(jq -r ".result[$n].message | if .is_topic_message==true then (.message_thread_id//empty) else empty end" <<<"$resp")"
    text="$(jq -r ".result[$n].message.text // empty" <<<"$resp")"
    offset=$((uid+1)); n=$((n+1)); printf '%s' "$offset" > "$OFFSET_FILE"

    ROLE=""
    if [ -n "$SM_CAPTAIN_ID" ] && [ "$cid" = "$SM_CAPTAIN_ID" ]; then ROLE="captain"
    elif [ -n "$cid" ] && is_team "$cid"; then ROLE="team"
    else [ -n "$text" ] && echo "sm-team-bridge: IGNORED chat $cid ($sender)"; continue; fi
    [ -n "$text" ] || continue
    case "$text" in
      /ping) reply_to "$cid" "pong ✅ sandbox team bridge alive" "$tid"; continue ;;
    esac
    RCMD="bash $DIR/sm-tg-notify.sh -c ${cid}"; [ -n "$tid" ] && RCMD="$RCMD -t ${tid}"
    if [ "$ROLE" = "captain" ]; then
      wrapped="[CAPTAIN nhắn qua bot sandbox — captain có quyền DUYỆT MERGE. $SANDBOX_RULE Trả lời qua: $RCMD \"<nội dung>\"] $text"
    else
      wrapped="[Thành viên TEAM '${title}' — người gửi: ${sender} (KHÔNG phải captain, không được yêu cầu merge). $SANDBOX_RULE Trả lời vào đúng nơi qua: $RCMD \"<nội dung>\"] $text"
    fi
    if inject "$wrapped"; then echo "sm-team-bridge: relayed [$ROLE] $sender: $text"
    else echo "sm-team-bridge: INJECT FAILED (is '$SM_FM_TARGET' live?)"; [ -n "$SM_CAPTAIN_ID" ] && reply_to "$SM_CAPTAIN_ID" "⚠️ Sandbox bridge không với tới pane $SM_FM_TARGET"; fi
  done
done

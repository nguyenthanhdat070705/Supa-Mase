#!/usr/bin/env bash
# Outbound v2: send a message from the first mate via Telegram.
# Usage: fm-tg-notify.sh "<message>"            -> captain DM (TG_CHAT_ID)
#        fm-tg-notify.sh -c <chat_id> "<message>" -> specific chat (e.g. work group)
set -eu
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${FM_TG_ENV:-$DIR/telegram.env}"
[ -f "$ENV_FILE" ] || { echo "fm-tg-notify: missing $ENV_FILE" >&2; exit 1; }
# shellcheck disable=SC1090
. "$ENV_FILE"
: "${TG_BOT_TOKEN:?TG_BOT_TOKEN not set}"
: "${TG_CHAT_ID:?TG_CHAT_ID not set}"

DEST="$TG_CHAT_ID"
if [ "${1:-}" = "-c" ]; then
  DEST="${2:?fm-tg-notify: -c needs a chat id}"
  shift 2
fi
MSG="$*"
[ -n "$MSG" ] || { echo "fm-tg-notify: empty message" >&2; exit 1; }
# Telegram hard-caps a message at 4096 chars; truncate defensively.
if [ "${#MSG}" -gt 3900 ]; then MSG="${MSG:0:3900}…"; fi
curl -fsS --max-time 20 \
  "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${DEST}" \
  --data-urlencode "text=${MSG}" \
  -o /dev/null && echo "sent" || { echo "fm-tg-notify: send failed" >&2; exit 1; }

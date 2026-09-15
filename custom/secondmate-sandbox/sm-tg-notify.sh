#!/usr/bin/env bash
# Outbound for the sandbox secondmate — sends via the TEAM bot (SM_BOT_TOKEN), never firstmate's.
# Usage: sm-tg-notify.sh -c <chat_id> [-t <topic_id>] "<message>"
set -eu
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SM_TG_ENV:-$DIR/sm-telegram.env}"
[ -f "$ENV_FILE" ] || { echo "sm-tg-notify: missing $ENV_FILE" >&2; exit 1; }
# shellcheck disable=SC1090
. "$ENV_FILE"
: "${SM_BOT_TOKEN:?SM_BOT_TOKEN not set}"
DEST=""; TOPIC=""
while :; do case "${1:-}" in
  -c) DEST="${2:?-c needs chat id}"; shift 2 ;;
  -t) TOPIC="${2:?-t needs topic id}"; shift 2 ;;
  *) break ;; esac; done
[ -n "$DEST" ] || { echo "sm-tg-notify: -c <chat_id> required" >&2; exit 1; }
MSG="$*"; [ -n "$MSG" ] || { echo "empty message" >&2; exit 1; }
[ "${#MSG}" -gt 3900 ] && MSG="${MSG:0:3900}…"
ARGS=( --data-urlencode "chat_id=${DEST}" --data-urlencode "text=${MSG}" )
[ -n "$TOPIC" ] && ARGS+=( --data-urlencode "message_thread_id=${TOPIC}" )
curl -fsS --max-time 20 "https://api.telegram.org/bot${SM_BOT_TOKEN}/sendMessage" "${ARGS[@]}" -o /dev/null && echo sent || { echo "send failed" >&2; exit 1; }

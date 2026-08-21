#!/usr/bin/env bash
# Helper: after you message your bot once, run this to discover your chat id.
set -eu
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${FM_TG_ENV:-$DIR/telegram.env}"
# shellcheck disable=SC1090
. "$ENV_FILE"
: "${TG_BOT_TOKEN:?TG_BOT_TOKEN not set}"
echo "Recent chats that messaged this bot:"
curl -fsS "https://api.telegram.org/bot${TG_BOT_TOKEN}/getUpdates" \
  | jq -r '.result[].message.chat | "  chat_id=\(.id)  name=\(.first_name // "")  username=@\(.username // "-")"' \
  | sort -u
echo "Put the right chat_id into TG_CHAT_ID in telegram.env"

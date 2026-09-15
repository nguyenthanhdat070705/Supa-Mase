#!/usr/bin/env bash
# Secondmate only: no firstmate fm-up, gate daemon, cron or primary mounts.
set -euo pipefail
umask 077
export HOME=/home/nguye FM_HOME=/home/nguye/team-sandbox
export PATH="/opt/codex/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
test "$(id -u)" != 0 || { echo 'Run secondmate as image user nguye'; exit 1; }
test -f "$FM_HOME/.fm-secondmate-home"
test "$(cat "$FM_HOME/.fm-secondmate-home")" = team-sandbox
test -f "$FM_HOME/data/charter.md"
test -d "$HOME/provisioner/.git"
test -f "$HOME/.codex/auth.json"
test -f "$FM_HOME/data/secondmate-transport.md"
command -v codex >/dev/null
command -v tmux >/dev/null
mkdir -p "$FM_HOME/state/secondmate-telegram"
exec 9>"$FM_HOME/state/secondmate-telegram/service.lock"
flock -n 9 || { echo 'Secondmate service is already running'; exit 1; }
rm -f -- "$FM_HOME/state/secondmate-telegram/ready.json"
# The isolated socket namespace cannot attach to the firstmate session.
if tmux -L secondmate has-session -t team-sandbox 2>/dev/null; then
  echo 'Existing team-sandbox session found; inspect it before restarting service'; exit 1
fi
tmux -L secondmate new-session -d -s team-sandbox -n secondmate -c "$FM_HOME" \
  'exec python3 /opt/secondmate/bridge.py launch'
tmux -L secondmate set-option -t team-sandbox remain-on-exit on
python3 /opt/secondmate/bridge.py run &
bridge_pid=$!
stop(){ kill "$bridge_pid" 2>/dev/null || true; tmux -L secondmate kill-server 2>/dev/null || true; }
trap stop EXIT TERM INT
wait "$bridge_pid"

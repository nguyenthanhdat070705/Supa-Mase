#!/usr/bin/env bash
# Start the inbound bridge as a tmux window in the first mate's session.
set -eu
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION="${TG_TMUX_SESSION:-firstmate}"
if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "tmux session '$SESSION' not found. Start it first:  tmux new -s $SESSION"
  exit 1
fi
tmux new-window -t "$SESSION" -n tg-bridge "bash '$DIR/fm-tg-bridge.sh'; echo bridge exited; sleep 5"
echo "Started 'tg-bridge' window in tmux session '$SESSION'. Attach: tmux attach -t $SESSION"

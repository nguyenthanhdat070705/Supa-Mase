#!/usr/bin/env bash
# Persistent child only; primary lifecycle belongs to the host control service.
set -euo pipefail
umask 077
export HOME=/home/nguye
export PATH="/opt/codex/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
export PYTHONPATH=/opt/secondmate
test "$(id -u)" != 0 || { echo 'Run secondmate as image user nguye'; exit 1; }
mapfile -t instance < <(python3 - <<'PY'
import json
from bridge import Settings
cfg = Settings.load()
parent = (cfg.home / '.fm-secondmate-parent').read_text().splitlines()
binding = json.loads((cfg.home / 'data/parent-control-binding.json').read_text())
assert (cfg.home / '.fm-secondmate-home').read_text().strip() == cfg.child_id
assert 'schema=fm-secondmate-parent.v1' in parent and 'route=remote' in parent
assert not any(line.startswith('parent_home=') for line in parent)
assert binding.get('schema') == 'parent-control-binding.v1'
assert binding.get('parent_id') == cfg.parent_id
assert binding.get('child_id') == cfg.child_id
assert binding.get('parent_url') == cfg.parent_url
print(cfg.home)
print(cfg.socket)
print(cfg.child_id)
PY
)
test "${#instance[@]}" -eq 3 || { echo 'Invalid instance or real parent binding'; exit 1; }
export FM_HOME="${instance[0]}"
socket="${instance[1]}"
child_id="${instance[2]}"
test -f "$FM_HOME/data/charter.md"
test -f "$HOME/.codex/auth.json"
test -f "$FM_HOME/data/secondmate-transport.md"
command -v codex >/dev/null
command -v tmux >/dev/null
mkdir -p "$FM_HOME/state/secondmate-telegram"
exec 9>"$FM_HOME/state/secondmate-telegram/service.lock"
flock -n 9 || { echo 'Secondmate service is already running'; exit 1; }
rm -f -- "$FM_HOME/state/secondmate-telegram/ready.json"
# The isolated socket namespace cannot attach to the firstmate session.
if tmux -L "$socket" has-session -t "$child_id" 2>/dev/null; then
  echo 'Existing child session found; inspect it before restarting service'; exit 1
fi
tmux -L "$socket" new-session -d -s "$child_id" -n secondmate -c "$FM_HOME" \
  'exec python3 /opt/secondmate/bridge.py launch'
tmux -L "$socket" set-option -t "$child_id" remain-on-exit on
python3 /opt/secondmate/bridge.py run &
bridge_pid=$!
stop(){ kill "$bridge_pid" 2>/dev/null || true; tmux -L "$socket" kill-server 2>/dev/null || true; }
trap stop EXIT TERM INT
wait "$bridge_pid"

#!/usr/bin/env bash
# firstmate container entrypoint — reproduces the WSL "fm-up" boot on a Linux server.
# Keeps PID 1 alive; the agents live inside the tmux session `firstmate`.
set -u
export HOME=/home/nguye
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

log(){ echo "[entrypoint $(date '+%F %T')] $*"; }

# 0) sanity: state volumes present?
[ -d "$HOME/firstmate" ]      || log "WARN: ~/firstmate not mounted — memory missing"
[ -x "$HOME/.local/bin/fm-up" ] || { log "FATAL: fm-up not mounted at ~/.local/bin/fm-up"; exec sleep infinity; }

# 1) no-mistakes daemon (binary + state live in the mounted ~/.no-mistakes volume)
if [ -x "$HOME/.no-mistakes/bin/no-mistakes" ]; then
  ( "$HOME/.no-mistakes/bin/no-mistakes" daemon run --root "$HOME/.no-mistakes" >>"$HOME/.no-mistakes/daemon.container.log" 2>&1 & )
  log "no-mistakes daemon started"
fi

# 2) recurring jobs via supercronic (container-native cron, runs as this user)
if [ -f "$HOME/crontab.container" ]; then
  ( supercronic "$HOME/crontab.container" >>"$HOME/firstmate/data/supercronic.log" 2>&1 & )
  log "supercronic started from ~/crontab.container"
fi

# 3) bring up tmux + first mate + telegram bridge (idempotent)
"$HOME/.local/bin/fm-up" || log "WARN: fm-up returned non-zero"
log "fm-up done — attach with:  docker exec -it firstmate tmux attach -t firstmate"

# 4) keep PID 1 alive as long as the tmux server lives; relaunch guidance on exit
while tmux has-session -t firstmate 2>/dev/null; do sleep 30; done
log "tmux session ended — sleeping so 'docker restart' or exec can recover"
exec sleep infinity

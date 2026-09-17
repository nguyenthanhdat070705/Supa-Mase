#!/usr/bin/env bash
# Restore a firstmate migration bundle on the NEW Linux server, next to docker-compose.yml.
#   ./fm-restore.sh firstmate-migration-YYYYMMDD.tar.gz        # plain
#   ./fm-restore.sh firstmate-migration-YYYYMMDD.tar.gz.gpg    # encrypted (prompts passphrase)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARC="${1:?usage: fm-restore.sh <archive.tar.gz[.gpg]>}"
[ -f "$ARC" ] || { echo "not found: $ARC"; exit 1; }

TAR="$ARC"
if [[ "$ARC" == *.gpg ]]; then
  command -v gpg >/dev/null || { echo "need gpg to decrypt"; exit 1; }
  TAR="${ARC%.gpg}"
  gpg -o "$TAR" -d "$ARC"
fi

echo "== Unpacking state + secrets into $HERE =="
tar -C "$HERE" -xzf "$TAR"
# fix ownership to the container user (UID 1000) so the non-root process can write
ROOT=()
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null || { echo "need root or sudo to set restored ownership"; exit 1; }
  ROOT=(sudo)
fi
"${ROOT[@]}" chown -R 1000:1000 "$HERE/state" "$HERE/secrets"
chmod -R go-rwx "$HERE/secrets" 2>/dev/null || true
if [ -f "$HERE/state/firstmate-control/client.json" ]; then
  "${ROOT[@]}" chown root:root "$HERE/state/firstmate-control/client.json"
  "${ROOT[@]}" chmod 0444 "$HERE/state/firstmate-control/client.json"
fi
if [ -f "$HERE/secrets/firstmate-control/parent.token" ]; then
  "${ROOT[@]}" chown 1000:1000 "$HERE/secrets/firstmate-control/parent.token"
  "${ROOT[@]}" chmod 0400 "$HERE/secrets/firstmate-control/parent.token"
fi

cat <<'EOF'

Restored. Next:
  1) docker compose build          # build the toolchain image
  2) docker compose up -d          # start firstmate (first mate + bridge + cron)
  3) docker exec -it firstmate tmux attach -t firstmate    # watch it boot

If the agents show a login prompt (OAuth tokens do not always survive a machine move):
  - Codex : docker exec -it firstmate codex   -> then /login  (ChatGPT account)
  - Claude: docker exec -it firstmate claude  -> then /login  (Claude Max account)
  - GitHub: docker exec -it firstmate gh auth status   (re-run `gh auth login` per account if needed)
Then restart the first mate window:  docker exec -it firstmate ~/.local/bin/fm-up
EOF

#!/usr/bin/env bash
# Build the firstmate migration bundle FROM the current WSL host.
# Lays out ./state and ./secrets exactly as docker-compose.yml expects, then tars
# them into one archive (optionally gpg-encrypted, since it contains credentials).
#
#   ./fm-pack.sh              # assemble ./state + ./secrets and make the tar
#   ./fm-pack.sh --encrypt    # additionally gpg symmetric-encrypt the tar (recommended)
#
# Excludes disposables: ~/.treehouse (worktrees), node_modules, venvs, caches,
# transcripts, runtime lock/pid/socket files.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
H="$HOME"
DATE="$(date +%Y%m%d)"
ENCRYPT=0; [ "${1:-}" = "--encrypt" ] && ENCRYPT=1

RSYNC='rsync -a --delete'
command -v rsync >/dev/null || { echo "need rsync (sudo apt install rsync)"; exit 1; }

echo "== 1. STATE (memory / gate / bridge) =="
mkdir -p "$HERE/state"
$RSYNC \
  --exclude 'projects/*/node_modules' --exclude 'projects/**/__pycache__' \
  --exclude 'projects/*/.venv' --exclude '**/*.pyc' \
  --exclude 'state/*.lock' --exclude 'state/*.pid' \
  "$H/firstmate/" "$HERE/state/firstmate/"
$RSYNC \
  --exclude 'daemon.lock' --exclude 'daemon.pid' --exclude 'socket' \
  --exclude '*.log' --exclude 'worktrees/*' \
  "$H/.no-mistakes/" "$HERE/state/no-mistakes/"
$RSYNC "$H/fm-telegram/" "$HERE/state/fm-telegram/"
cp "$H/.fm-primary"            "$HERE/state/fm-primary"
cp "$H/.local/bin/fm-up"       "$HERE/state/fm-up"
cp "$HERE/crontab.container"   "$HERE/state/crontab.container"

echo "== 2. SECRETS (auth) — sensitive =="
mkdir -p "$HERE/secrets"
$RSYNC "$H/.codex/" "$HERE/secrets/codex/"
# claude: keep credentials + custom skills + settings; drop bulky transcripts/caches
$RSYNC \
  --exclude 'projects/*' --exclude 'todos/*' --exclude 'shell-snapshots/*' \
  --exclude 'history.jsonl' --exclude 'statsig/*' --exclude '.cache/*' \
  "$H/.claude/" "$HERE/secrets/claude/"
mkdir -p "$HERE/secrets/gh"; $RSYNC "$H/.config/gh/" "$HERE/secrets/gh/"

echo "== 3. Archive =="
TAR="$HERE/firstmate-migration-$DATE.tar.gz"
tar -C "$HERE" -czf "$TAR" state secrets
chmod 600 "$TAR"
SZ=$(du -h "$TAR" | cut -f1)
echo "   -> $TAR ($SZ)"

if [ "$ENCRYPT" = 1 ]; then
  command -v gpg >/dev/null || { echo "gpg not found; leaving plain tar (chmod 600)"; exit 0; }
  gpg --symmetric --cipher-algo AES256 -o "$TAR.gpg" "$TAR"
  shred -u "$TAR" 2>/dev/null || rm -f "$TAR"
  echo "   -> ENCRYPTED: $TAR.gpg  (plain tar shredded)"
  echo "   Decrypt on server:  gpg -o firstmate-migration-$DATE.tar.gz -d firstmate-migration-$DATE.tar.gz.gpg"
fi

cat <<EOF

DONE. Move to the new server, then restore:
  scp $(basename "$TAR")${ENCRYPT:+.gpg}  user@server:/opt/firstmate/
  # on the server, inside the fm-docker dir:  ./fm-restore.sh <archive>
Remember: the archive holds credentials — transfer over scp/ssh only, never a public link.
EOF

#!/usr/bin/env bash
# Robot đổ số sáng MayCha — cron 8h15 VN (01:15 UTC) + @reboot chạy bù.
# Mac chốt 28/8/2026 phương án cron local. Thiết kế: data/dp-sync-daily-2808/report.md.
set -uo pipefail
ROBOT=/home/nguye/firstmate/data/dp-morning-robot
mkdir -p "$ROBOT/logs"
LOG="$ROBOT/logs/run-$(date +%Y%m%d-%H%M%S).log"

# --boot: lượt @reboot — chỉ chạy bù nếu đã qua 08:15 VN (trước giờ đó cron 8h15 sẽ tự lo)
if [ "${1:-}" = "--boot" ]; then
  hhmm=$(TZ=Asia/Ho_Chi_Minh date +%H%M)
  [ "$hhmm" \> "0814" ] || { echo "boot truoc 08:15 VN ($hhmm) - de cron 8h15 lo" >>"$LOG"; exit 0; }
fi

# §3 chống chạy trùng (cron + chạy bù tay)
exec 9>/home/nguye/firstmate/state/dp-morning-sync.lock
flock -n 9 || { echo "da co luot dang chay, bo qua" >>"$LOG"; exit 0; }

# Telegram cho worker tg() tự nhắn (thiếu file thì chuỗi vẫn chạy, chỉ không nhắn)
set -a; source /home/nguye/fm-telegram/telegram.env 2>/dev/null || true; set +a

# Code mới nhất từ bản chính GitHub; lỗi mạng thì chạy bản đang có
git -C "$ROBOT/repo" pull --ff-only >>"$LOG" 2>&1 || echo "[robot] pull fail - dung ban hien co" >>"$LOG"

PYTHONUNBUFFERED=1 python3 "$ROBOT/run_morning.py" >>"$LOG" 2>&1
rc=$?
echo "[robot] exit=$rc" >>"$LOG"

# giữ 30 log gần nhất
ls -1t "$ROBOT"/logs/run-*.log 2>/dev/null | tail -n +31 | xargs -r rm --
exit $rc

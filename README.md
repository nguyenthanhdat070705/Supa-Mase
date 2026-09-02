# MayCha Firstmate — bản lưu trữ code tùy chỉnh (`maycha-custom`, cập nhật 3/9/2026)

Nhánh này lưu **toàn bộ code tùy chỉnh** của hệ firstmate MayCha, tách khỏi bản mirror distro (nhánh `main` và các nhánh `fm/*` trong repo này là mirror của `kunchenguid/firstmate` + fork `omnimaycha/firstmate`).

**KHÔNG có secret nào ở đây** (cố ý): `telegram.env` thật (token bot), `.offset`, `inbox/`, và toàn bộ ký ức firstmate (`~/firstmate/data/` — backlog, captain.md, credentials) chỉ nằm trên máy, không đưa lên GitHub vì repo này public.

## Cấu trúc

```
custom/
├── fm-telegram/            # Cầu Telegram (bridge v2 - hỗ trợ group)
│   ├── fm-tg-bridge.sh     # Inbound: DM captain (full quyền) + group (chỉ hỏi/xem)
│   ├── fm-tg-notify.sh     # Outbound: mặc định DM captain; -c <chat_id> để trả lời group
│   ├── start.sh
│   ├── fm-tg-whoami.sh
│   └── telegram.env.example
└── claude-skills/          # Skill tự viết, cài ở ~/.claude/skills/ trong WSL
    ├── xu-ly-data-lon/     # Luật chống OOM (VM 7.2GB): SQL pushdown, đọc chunk
    ├── bao-cao-so-sang/    # Format báo số cho captain: đối soát trước, báo sau
    ├── dp-app-checklist/   # Checklist nghiệm thu màn app Demand Planning
    └── no-mistakes/        # Gọi pipeline no-mistakes để gate thay đổi
```

## Khôi phục trên máy mới (disaster recovery)

1. **Cài firstmate distro:** `git clone https://github.com/kunchenguid/firstmate ~/firstmate` (hoặc từ nhánh `main` repo này), cài `tmux`, `gh` (đăng nhập), `jq`, `claude`.
2. **Cầu Telegram:**
   ```sh
   mkdir -p ~/fm-telegram && cp -r custom/fm-telegram/* ~/fm-telegram/
   cp ~/fm-telegram/telegram.env.example ~/fm-telegram/telegram.env
   # điền TG_BOT_TOKEN, TG_CHAT_ID (DM captain), TG_FM_TARGET=firstmate:0,
   # TG_GROUP_IDS (id các group, cách nhau dấu phẩy) vào telegram.env
   chmod +x ~/fm-telegram/*.sh
   ```
3. **Skill:** `mkdir -p ~/.claude/skills && cp -r custom/claude-skills/* ~/.claude/skills/`
4. **Khởi động:**
   ```sh
   tmux new-session -d -s firstmate -c ~/firstmate
   tmux rename-window -t firstmate:0 primary
   tmux send-keys -t firstmate:0 "claude --model claude-fable-5 --dangerously-skip-permissions" Enter
   tmux new-window -t firstmate:1 -n tg-bridge -c ~/fm-telegram "bash -c 'bash ~/fm-telegram/fm-tg-bridge.sh; echo bridge exited; sleep 5'"
   ```
5. Ký ức (`~/firstmate/data/`) khôi phục từ backup riêng (không nằm trên GitHub).

## Ghi chú vận hành đã đúc kết

- VM WSL 7.2GB RAM — job data phải theo skill `xu-ly-data-lon`, không là OOM-kill.
- Login Claude trong WSL hết hạn ~2 tuần/lần: `tmux attach -t firstmate` → `/login`.
- Bridge tăng offset trước khi inject: tin text fail là mất (chỉ ảnh/file được lưu `inbox/`).
- Cập nhật distro: nhắn firstmate `/updatefirstmate` (fast-forward an toàn).

## Cập nhật 3/9/2026 — tái cấu trúc hạm đội
- Primary = **Claude Fable 5.1 + ultracode** (chỉ preview/plan/review); xem `custom/autostart/fm-primary.example`.
- `custom/autostart/fm-up`: bộ khởi động 1 lệnh (tmux + primary + bridge) kèm **kickoff ultracode** mỗi phiên; chạy từ `firstmate-autostart.vbs` (Windows Startup) lúc logon.
- `custom/autostart/crontab.txt` + `dp-morning-robot-run.sh`: robot đổ số sáng 08:45 VN (code robot pull từ repo demand-planning-maycha).
- `custom/firstmate-config/crew-dispatch.json`: Sol xhigh = task khó; Terra xhigh = task dễ; Luna xhigh = việc vặt + fallback.
- Skills giờ có 8 (thêm doi-soat-so-lieu, railway-deploy-antoan, supabase-schema, ui-chuan-maycha).
- Bridge v2 + topic-support: trả lời đúng topic forum-group; captain nhắn trong group = toàn quyền.

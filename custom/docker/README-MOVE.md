# Di chuyển firstmate sang máy Linux 24/7 bằng Docker

Bộ này đóng gói firstmate thành container để chạy trên **server/PC Linux luôn bật**.
Điểm cốt lõi: **image chỉ chứa toolchain** (Ubuntu + node + codex + claude + tmux + gh + supercronic);
còn **ký ức và bí mật đi riêng** qua volume, khôi phục từ gói di cư — image không bao giờ chứa credentials.

```
fm-docker/
├── Dockerfile            # toolchain: node22, codex 0.154, claude, tmux, gh, supercronic
├── docker-compose.yml    # service firstmate + volume state/secrets + restart:unless-stopped
├── entrypoint.sh         # dựng no-mistakes daemon + cron + tmux/fm-up, giữ container sống
├── crontab.container     # cron đã dọn (robot sáng 08:45; bỏ dòng /mnt/c và .treehouse)
├── fm-pack.sh            # (máy CŨ) đóng gói state+secrets thành 1 archive
├── fm-restore.sh         # (máy MỚI) bung archive + hướng dẫn re-auth
└── state/ , secrets/     # do fm-pack/fm-restore tạo — KHÔNG commit
```

## Trên máy CŨ (WSL hiện tại)

```bash
cd ~/fm-docker
./fm-pack.sh --encrypt          # tạo firstmate-migration-YYYYMMDD.tar.gz.gpg (nhập passphrase)
```

Gói này gồm: `~/firstmate` (ký ức data/, config/, projects/), `~/.no-mistakes` (gate+sqlite),
`~/fm-telegram` (bridge), và bí mật (`.codex`, `.claude` creds+skills, `gh`).
**Bỏ qua** `~/.treehouse` (4.6GB worktree tạm — không cần), node_modules, cache, transcripts.

## Chuyển sang server

```bash
scp firstmate-migration-*.tar.gz.gpg  user@server:/opt/firstmate/
# và copy cả thư mục fm-docker (Dockerfile, compose, script) lên /opt/firstmate/
```
(hoặc trên server: `git clone` nhánh `maycha-custom` của Supa-Mase — bộ fm-docker nằm ở `custom/docker/`)

## Trên máy MỚI (Linux server)

```bash
# 0) cài Docker Engine nếu chưa có
curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER   # đăng nhập lại

cd /opt/firstmate/fm-docker
./fm-restore.sh firstmate-migration-YYYYMMDD.tar.gz.gpg   # bung state+secrets
docker compose build          # dựng image toolchain (~vài phút)
docker compose up -d          # khởi động firstmate
docker exec -it firstmate tmux attach -t firstmate        # xem nó boot (thoát: Ctrl+B rồi D)
```

## ⚠️ Bước bắt buộc sau khi move: đăng nhập lại

Token OAuth (ChatGPT của Codex, Claude Max) **gắn với thiết bị/phiên**, thường KHÔNG sống sót
qua máy mới. Sau `up -d`, nếu agent hiện màn hình `/login`:

| Thành phần | Lệnh | Ghi chú |
|---|---|---|
| Codex (Astra 6) | `docker exec -it firstmate codex` → `/login` | tài khoản ChatGPT Pro |
| Claude (nếu dùng làm primary) | `docker exec -it firstmate claude` → `/login` | Claude Max |
| GitHub | `docker exec -it firstmate gh auth status` | 2 tài khoản; `gh auth login` lại nếu cần |

gh token là PAT nên thường copy được; OAuth ChatGPT/Claude gần như chắc phải `/login` lại.
Sau khi login xong: `docker exec -it firstmate ~/.local/bin/fm-up` để khởi động lại first mate.

## Vận hành hằng ngày trên server

```bash
docker exec -it firstmate tmux attach -t firstmate   # vào buồng lái
docker compose logs -f firstmate                     # xem log entrypoint
docker compose restart firstmate                     # khởi động lại (state giữ nguyên)
docker exec -it firstmate ~/firstmate/data/dp-morning-robot/run.sh --boot   # chạy bù số sáng
```

Telegram bridge chạy sẵn trong container → bạn vẫn chỉ huy qua điện thoại như cũ.
Cập nhật khung: `docker exec -it firstmate bash -lc 'cd ~/firstmate && bin/fm-update.sh'`.

## Khác biệt so với bản WSL (cố ý)

- Không còn autostart kiểu Windows `.vbs`; thay bằng `restart: unless-stopped` của Docker — server reboot thì container tự lên.
- Cron chạy bằng **supercronic** (non-root, chuẩn container) thay cho system cron.
- Dòng cron `/mnt/c/...build_sales_mix_precalc.py` (đường dẫn Windows) đã bỏ — nếu vẫn cần, chép script vào `~/firstmate` và thêm lại vào `crontab.container`.
- no-mistakes chạy trong container; nếu cổng gọi Docker/Railway thì mở thêm cấu hình mạng tương ứng.

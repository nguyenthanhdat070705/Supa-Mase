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

Container MacBot không được mount `/var/run/docker.sock`. Compose chỉ thêm
`host.docker.internal:host-gateway` để client có thể gọi broker quản trị giới
hạn trên host; broker phải bind vào đúng địa chỉ bridge riêng, được firewall chỉ
cho các network bot. Container chạy non-root, bỏ toàn bộ Linux capabilities,
bật `no-new-privileges`, giới hạn PID và xoay log `10m x 3`. Sau khi recreate,
xác minh từ trong container rằng socket Docker không tồn tại và một request có
token tới broker hoạt động; không chép operator token vào MacBot.

Đo RAM/CPU thật của host rồi tạo `.env` mode 0600 với hai giá trị bắt buộc
`FIRSTMATE_MEMORY_LIMIT` và `FIRSTMATE_CPUS`. Chọn chúng sao cho tổng trần của
MacBot (ví dụ cú pháp `8g`, `4.0`), Fin (6 GiB/3 CPU), Toan (4 GiB/2 CPU), broker
và phần dự phòng hệ điều hành không vượt tài nguyên vật lý. Compose đặt
`memswap_limit` bằng `mem_limit`; không được bỏ biến để MacBot chạy không giới
hạn. Con số ví dụ không phải khuyến nghị cho host chưa đo.

Trước khi `docker compose up`, cài mã broker đã merge dưới
`/usr/local/lib/firstmate-control/parent-control` với owner root và không cho
agent ghi. Tạo `state/firstmate-control/client.json` từ
`parent-client.example.json` (root-owned, 0444) và đặt **chỉ parent token** tại
`secrets/firstmate-control/parent.token` (UID 1000, mode 0400). Ba bind đều
read-only và `create_host_path: false`, nên thiếu file/path sẽ làm deployment
fail closed. Không đặt operator token trong cây `custom/docker`.

Sau khi broker và đúng hai manifest đã được kích hoạt, kiểm tra từ MacBot:

```sh
docker exec --user 1000:1000 firstmate sh -lc '
  test ! -S /var/run/docker.sock && test ! -S /run/docker.sock &&
  FM_HOME=/home/nguye/firstmate python3 /opt/firstmate-control-client/client.py \
    --config /etc/firstmate-control/client.json list'
```

Kết quả phải chỉ có `team-sandbox` và `toanmytran-bot`, cả hai mang cờ
`administration: true`. Tiếp theo chạy `diagnostics` cho từng ID; không dùng
`control`, `resources` hay `backup` làm smoke test. Cờ `administration: true`
chỉ cho biết bot nằm trong allowlist quản trị; Toan vẫn không được khởi động khi
`start_allowed: false`.

Telegram bridge chạy sẵn trong container → bạn vẫn chỉ huy qua điện thoại như cũ.
Cập nhật khung: `docker exec -it firstmate bash -lc 'cd ~/firstmate && bin/fm-update.sh'`.

## Khác biệt so với bản WSL (cố ý)

- Không còn autostart kiểu Windows `.vbs`; thay bằng `restart: unless-stopped` của Docker — server reboot thì container tự lên.
- Cron chạy bằng **supercronic** (non-root, chuẩn container) thay cho system cron.
- Dòng cron `/mnt/c/...build_sales_mix_precalc.py` (đường dẫn Windows) đã bỏ — nếu vẫn cần, chép script vào `~/firstmate` và thêm lại vào `crontab.container`.
- no-mistakes chạy trong container; nếu cổng gọi Docker/Railway thì mở thêm cấu hình mạng tương ứng.

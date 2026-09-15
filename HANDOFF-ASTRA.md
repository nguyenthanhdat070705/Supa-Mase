# BÀN GIAO TOÀN CẢNH HỆ FIRSTMATE MAYCHA
> Cập nhật 15/09/2026 · Người soạn: Claude (trợ lý hạ tầng phía máy captain) · Người đọc: **Astra 6 (first mate)**
> Mục đích: cho bạn (Astra 6) nắm đầy đủ bối cảnh hệ thống, lịch sử, ranh giới và việc đang chờ.

---

## 0. TÓM TẮT 30 GIÂY

Bạn là **Astra 6** — first mate, chạy `codex --model gpt-6-astra` effort **max**, YOLO mode, trong **Docker container `firstmate` trên server Linux 24/7** (không còn ở laptop WSL của captain nữa — đã di cư ngày 15/09/2026).
Vai trò captain chốt: **CHỈ preview, lên plan, review/verify kết quả crewmate — KHÔNG tự code trực tiếp.**
Captain chỉ huy bạn qua **Telegram**. Có 1 việc lớn đang chờ bạn: **dựng secondmate sandbox cho team** (xem §8).

---

## 1. KIẾN TRÚC HIỆN TẠI

```
Server Linux 24/7 (user: dat, hostname ttvhb, Tailscale 100.68.195.89)
└── Docker container "firstmate"  (restart: unless-stopped → server reboot tự lên)
    ├── tmux session "firstmate"
    │   ├── window 0 (node) = BẠN — codex gpt-6-astra max, YOLO, cwd ~/firstmate
    │   └── window 1 (tg-bridge) = cầu Telegram
    ├── supercronic  → robot đổ số sáng 08:45 VN
    └── no-mistakes daemon v1.57 → cổng chất lượng, gác 4 repo
```

**Thư mục (trong container):**
| Đường dẫn | Nội dung |
|---|---|
| `/home/nguye/firstmate` | Nhà chính: `AGENTS.md`, `data/` (ký ức), `state/`, `config/`, `projects/` |
| `/home/nguye/fm-telegram` | Cầu Telegram: `fm-tg-bridge.sh`, `fm-tg-notify.sh`, `telegram.env`, `inbox/` |
| `/home/nguye/.no-mistakes` | Cổng chất lượng: binary + sqlite + repos |
| `/home/nguye/.claude/skills` | 8 skill nghiệp vụ (xem §5) |
| `~/.npm-global/bin/codex` | Binary codex bạn đang chạy (user-prefix, xem §6 bẫy #8) |

**Trên host server:** `~/fm-docker/` chứa `Dockerfile`, `docker-compose.yml`, `entrypoint.sh`, và `state/` + `secrets/` được mount vào container. Ký ức nằm ở `state/firstmate/` — **đây là linh hồn, phải backup**.

---

## 2. ĐỘI HÌNH MODEL (captain chốt 14/09/2026)

| Vai | Model | Effort |
|---|---|---|
| **First mate (bạn)** | `gpt-6-astra` | **max** |
| Crew — việc khó | `gpt-5.6-sol` | xhigh |
| Crew — việc chuẩn (mặc định) | `gpt-5.6-terra` | xhigh |
| Crew — việc nhẹ | `gpt-5.3-codex-spark` | xhigh |
| Crew — fallback / worker "Minh" | `gpt-5.6-luna` | xhigh |

Nguồn sự thật: `config/crew-dispatch.json`. **Mọi worker chạy XHIGH**, không ngoại lệ (quy tắc 14/9 thay cho quy tắc MAX ngày 9/9). Riêng bạn giữ **max**.

**Tài khoản:** Codex dùng ChatGPT **Pro** (`hhoangtunganh.ai@gmail.com`). GitHub trong container có 2 tài khoản: `DemandPlanningMC` (active, cho repo app DP) và `omnimaycha` (fork firstmate + quyền ghi Supa-Mase — phải `gh auth switch -u omnimaycha` rồi switch lại).

---

## 3. KÊNH TELEGRAM (cách captain nói chuyện với bạn)

`fm-tg-bridge.sh` (bản v2 + hỗ trợ forum topic) poll Telegram rồi **gõ tin vào pane `firstmate:0`** của bạn:

| Nguồn tin | Quyền | Bạn phải làm gì |
|---|---|---|
| **DM của captain** | Toàn quyền | Nhận lệnh, trả lời qua `fm-tg-notify.sh "<nội dung>"` |
| **Captain nhắn trong group đã đăng ký** (`-1004338487347` — "Công Ty TNHH MAC") | Toàn quyền như DM | Trả lời **đúng topic**: `fm-tg-notify.sh -c <chat> -t <topic> "..."` |
| **Thành viên khác trong group** | CHỈ hỏi/xem | Trả lời câu hỏi số liệu; **từ chối lịch sự** mọi yêu cầu đổi hệ thống/dữ liệu/deploy |
| Chat lạ | Bị chặn | Bridge tự bỏ qua |

⚠️ **Giới hạn đã biết:** bridge tăng `.offset` TRƯỚC khi gõ tin — nếu gõ thất bại, **tin text mất luôn** (ảnh/file thì đã lưu ở `inbox/`).

---

## 4. LỊCH SỬ DI CƯ (vì sao mọi thứ như hiện nay)

| Mốc | Chuyện gì |
|---|---|
| ~07/2026 | Captain dựng firstmate trong WSL Ubuntu trên laptop ThinkPad |
| 06/08 | Sự cố OOM lớn; khung distro bị kẹt 96 commit vì commit lỡ nằm trên `main` |
| 21/08 | Bridge nâng v2 (hỗ trợ group); no-mistakes lên v1.53; thêm 4 skill; lập két backup GitHub |
| 28/08–01/09 | Thêm **autostart** (Windows vbs) + **robot đổ số sáng** chạy cron độc lập |
| 03/09 | Primary đổi Codex → Claude Fable 5.1 |
| 09/09 | Primary đổi lại → **Astra 6 max**; crew Sol/Terra/Luna |
| 10/09 | Nâng khung distro lên `b1ad702f` (+169 commit) |
| **15/09** | **DI CƯ sang server Linux 24/7 bằng Docker** — laptop nghỉ hưu hoàn toàn |

**Về việc di cư:** gói `state + secrets` được nén + mã hóa AES256 (2.4GB, đã loại 4.6GB worktree rác), chuyển qua scp, bung trên server. Token OAuth ChatGPT **sống sót**, không phải `/login` lại. Laptop cũ đã tắt firstmate + **vô hiệu autostart** (`firstmate-autostart.vbs.disabled`) để tránh 2 bot giành 1 bot Telegram.

---

## 5. TRANG BỊ THÊM NGOÀI DISTRO GỐC

1. **Cầu Telegram 2 chiều** (gốc không có) — captain chỉ huy qua điện thoại
2. **Robot đổ số sáng** `data/dp-morning-robot/run.sh` — cron 08:45 VN, thuần Python, không tốn token; số sáng ra kể cả khi bạn ngủ
3. **no-mistakes v1.57** — gác 4 repo; thống kê: 86 lỗi bị bắt, ~70% tự sửa, repo hưởng lợi nhất là demand-official
4. **8 skill** tại `~/.claude/skills/`: `xu-ly-data-lon`, `bao-cao-so-sang`, `dp-app-checklist`, `doi-soat-so-lieu`, `railway-deploy-antoan`, `supabase-schema`, `ui-chuan-maycha`, `no-mistakes`
5. **Két backup GitHub** — xem §7

---

## 6. CẨM NANG SỰ CỐ ĐÃ GẶP (đọc trước khi chẩn đoán)

| # | Triệu chứng | Nguyên nhân | Cách chữa |
|---|---|---|---|
| 1 | Bot im, job data chết | **OOM-kill** (laptop chỉ 7.2GB) | Chia chunk, đẩy GROUP BY xuống Supabase — skill `xu-ly-data-lon` |
| 2 | Bridge báo "relayed" nhưng bot không đáp | **Login hết hạn** (~2 tuần/lần thời Claude) | `/login` trong pane |
| 3 | Tin nằm trong ô soạn, không gửi | Tin tới lúc agent đang boot | `tmux send-keys -t firstmate:0 Enter` |
| 4 | `ps` đổi giờ khởi động, lỗi `0x8007274c` | **Clock drift WSL2** sau khi máy ngủ | Thử lại; **không** `wsl --shutdown` |
| 5 | Mất sạch, tmux không tồn tại | VM WSL tắt theo laptop | Dựng lại primary → rồi mới bật bridge |
| 6 | Codex đứng im sau boot | Dialog "Update available" chặn | Chọn *Skip until next version* |
| 7 | **Codex tự sát** — pane rớt về bash trần, bridge gõ tin vào shell → `command not found` | Enter lạc chọn "Update now" → npm ghi `/usr` fail EACCES → Codex thoát | **Đã fix gốc:** npm prefix dời sang `~/.npm-global`, in-app update chạy được không cần sudo |
| 8 | `useradd: UID 1000 is not unique` khi build image | `ubuntu:24.04` có sẵn user UID 1000 | Dockerfile đã vá: xóa user cũ trước khi tạo `nguye` |
| 9 | `permission denied ... docker.sock` | User mới thêm vào nhóm docker, chưa hiệu lực | `newgrp docker` hoặc đăng nhập lại |
| 10 | Tin nhắn captain đi lạc, lúc được lúc không | **2 bridge cùng poll 1 bot** (laptop + server) | Chỉ cho 1 nơi chạy bridge |

---

## 7. REPO GITHUB

| Repo | Vai trò |
|---|---|
| `kunchenguid/firstmate` | **origin** — bản gốc tác giả, nơi kéo update (`bin/fm-update.sh`). Local đang ở `b1ad702f` |
| `omnimaycha/firstmate` | **fork** của captain — đẩy PR đóng góp ngược lên tác giả (đã có 4 nhánh `fm/*`) |
| `nguyenthanhdat070705/Supa-Mase` | **két backup của captain**: `main` = mirror distro; nhánh **`maycha-custom`** = toàn bộ đồ tùy chỉnh (bridge, 8 skill, fm-up, bộ Docker, autostart, robot glue, tài liệu này) |
| `DemandPlanningMC/demand-planning-maycha` | Repo app Demand Planning — robot sáng pull code từ đây |

⚠️ **Ký ức (`data/`) KHÔNG nằm trên GitHub** (gitignored) — chỉ tồn tại trên server. Đây là thứ duy nhất chưa có backup ngoài.

---

## 8. 🔴 VIỆC ĐANG CHỜ BẠN: DỰNG SECONDMATE SANDBOX CHO TEAM

Captain muốn **1 secondmate cho cả team tương tác**, với 3 ràng buộc cứng:

### Yêu cầu
1. **Team tương tác trực tiếp** với secondmate qua **một bot Telegram RIÊNG** (captain đang tạo qua @BotFather) — không dùng chung bot của bạn.
2. **Cách ly tuyệt đối với bạn (firstmate).** Secondmate có `FM_HOME` riêng; nó không bao giờ ghi vào nhà bạn; **bạn KHÔNG tự động tiếp thu** ký ức/kiến thức của nó.
3. **Merge phải có captain duyệt** — cả **kiến thức** lẫn **code**. Không có luồng tự động nào.

### Thông số captain đã chốt
- id: **`team-sandbox`**
- Óc điều phối: **`codex gpt-5.6-sol` xhigh** → đặt `config/secondmate-harness = "codex gpt-5.6-sol xhigh"`
- Lãnh địa (scope): sandbox cho team — trả lời nghiệp vụ, làm việc khám phá/thử nghiệm, **học từ team**
- Project: `demand-planning-maycha` ở tư thế **sandbox** — code làm trên nhánh tiền tố `sandbox/`, giao hàng bằng **PR (direct-PR)**, **cấm đẩy thẳng vào main**

### Bạn cần làm
Dùng skill `secondmate-provisioning`: scaffold charter → seed nhà riêng → đăng ký `data/secondmates.md` → launch.
Xong **báo captain qua Telegram 2 thứ**: (1) đường dẫn `FM_HOME`, (2) **tên tmux target pane** của agent secondmate (dạng `session:window`) — captain cần để nối cầu Telegram của team.

### Phần đã chuẩn bị sẵn cho bạn
Cầu Telegram riêng cho team đã viết xong, nằm ở Supa-Mase nhánh `maycha-custom`, thư mục `custom/secondmate-sandbox/`:
- `sm-team-bridge.sh` — poll bot team, **mọi thành viên team đều được giao việc**, captain có thêm quyền duyệt merge
- `sm-tg-notify.sh` — gửi qua bot team (không đụng bot của bạn)
- `sm-telegram.env.example` — mẫu config (`SM_BOT_TOKEN`, `SM_TEAM_GROUP_IDS`, `SM_FM_TARGET`, `SM_CAPTAIN_ID`)

Luật sandbox được **ép vào từng tin nhắn** bridge gõ sang secondmate, nên nó luôn được nhắc: *không merge/promote gì nếu captain chưa duyệt*.

### Quy trình merge sau này (captain vận hành)
- **Code:** sandbox chỉ ra PR trên nhánh `sandbox/` → captain xem → merge hoặc bỏ.
- **Kiến thức:** captain hỏi sandbox *"tóm tắt những gì học được"* → chọn cái đáng → nói với **bạn**: *"merge kiến thức X từ sandbox"* → lúc đó bạn mới ghi vào ký ức.

---

## 9. RANH GIỚI & QUY TẮC PHẢI GIỮ

1. **Bạn không tự code** — preview, plan, review; giao việc cho crew theo `crew-dispatch.json`.
2. **Mọi worker chạy xhigh**; bạn chạy max.
3. **Không tiếp thu bất cứ gì từ `team-sandbox`** trừ khi captain nói rõ từng món.
4. **Thành viên team (không phải captain) không có quyền** đổi hệ thống/dữ liệu/hạ tầng — từ chối lịch sự, hướng họ hỏi captain.
5. **Xử lý data lớn phải theo skill `xu-ly-data-lon`** (đẩy SQL xuống Supabase, đọc theo chunk) — đã có 3 vụ OOM vì bỏ qua luật này.
6. **Báo số cho captain phải đối soát trước** (skill `bao-cao-so-sang`); job chết thì **khai thẳng**, cấm gửi bảng trống hay số cũ.
7. **Cập nhật khung** bằng `bin/fm-update.sh` (fast-forward, không phá việc đang chạy) — không tự ý đổi cấu hình model khi captain chưa yêu cầu.

---

## 10. VIỆC DỌN DẸP CÒN TREO (không gấp)

- Ký ức `data/` chưa có backup ngoài server → nên có bản nén định kỳ (private).
- Máy laptop cũ còn cron robot sáng → có thể đổ số trùng với server (đã có khóa `flock` + `data_version` chặn, nên vô hại nhưng nên tắt).
- Máy laptop cũ còn `~/fm-docker/state` + `secrets` (~4.3GB bản plaintext) → xóa sau khi chắc server ổn.
- `config/secondmate-harness` đang là `codex gpt-5.3-codex-spark high` → phải đổi thành `codex gpt-5.6-sol xhigh` khi dựng `team-sandbox`.

---

*Hết. Có gì mâu thuẫn giữa tài liệu này và `data/captain.md`, ưu tiên `captain.md` và hỏi captain.*

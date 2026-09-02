---
name: railway-deploy-antoan
description: Quy trình deploy an toàn lên Railway cho app Demand Planning (project sales-sync-ttvh — service demand-app và worker). Dùng TRƯỚC MỖI lần deploy web hoặc worker, khi deploy fail, khi cần rollback, hoặc khi đổi env var trên Railway.
---

# Railway deploy an toàn — sales-sync-ttvh

Hạ tầng: Railway project **sales-sync-ttvh** (`d8e2944a`), env production (`5c6b3471`), region asia-southeast1. Services: **demand-app** (`3f80933b`, web https://demand-app-production.up.railway.app) và **worker** (`16b3dae1`, sync 8g15 VN). Restart policy ON_FAILURE, start command trong `app/railway.json` (uvicorn) và `worker/railway.json` (python main.py).

## Luật cứng trước khi deploy

1. **`railway up` phải chạy TỪ ĐÚNG THƯ MỤC SERVICE**: web từ `app/`, worker từ `worker/` (nơi có railway.json). Up từ repo root fail "railpack prepare exited with an error" (đã vấp 20/8).
2. **Deploy từ đĩa Linux**, không từ `/mnt/c` (upload hỏng).
3. Code đã land (commit + push GitHub) và qua nghiệm thu `dp-app-checklist` trước — không deploy code chưa commit.
4. **Đếm số lần deploy trong ngày:** mỗi lần deploy, worker boot chạy lượt sync bù + tải lại DataWarehouse qua Microsoft Graph; deploy dồn dập (≥4 lần/ngày) làm Microsoft trả 429 Too Many Requests (đã dính 10/8). Gom nhiều fix vào 1 lần deploy.
5. Env var đủ bộ trước khi bấm: `SUPABASE_DB_HOST/NAME/PORT` + user/pass, `SESSION_SECRET` (web), `TG_BOT_TOKEN`/`TG_CHAT_ID`, `MS_CLIENT_ID`, `DP_SYNC_*` (worker). Chỉ kiểm TÊN, không bao giờ ghi giá trị vào chat/commit.

## Điều cấm

- **CẤM chuyển worker sang Railway cron**: runtime V2/Metal giết container cron sau ~26–39s — worker phải là service thường trực tự giữ lịch trong process (sleep tới giờ chạy, sync bù khi boot). Đã verify 3/8.
- CẤM `serviceInstanceRedeploy` để "lên code mới" — nó chỉ redeploy snapshot cũ; code mới bắt buộc `railway up`. (Redeploy chỉ dùng để áp env var mới hoặc rollback snapshot.)
- CẤM báo "đã deploy" khi chưa tự mở lại domain production thật.

## Trình tự chuẩn

1. Land code → GitHub xong.
2. `(cd app && railway up)` hoặc `(cd worker && railway up)` — link đúng project/service/env nếu CLI hỏi.
3. Theo dõi build log tới khi deploy ACTIVE; build fail thì đọc log, đừng up lại mù.
4. **Xác nhận sống:** web → mở https://demand-app-production.up.railway.app, login, đảo 1–2 màn, console sạch; worker → xem log thấy lượt sync bù boot chạy, không lặp crash-restart.
5. Web có đổi số liệu → đối soát nhanh theo `doi-soat-so-lieu` trước khi báo captain.
6. Báo captain 1 dòng kết quả kèm link production.

## Khi hỏng — rollback

- Bản mới lỗi: rollback = redeploy snapshot deployment trước đó trên Railway (dashboard hoặc API `serviceInstanceRedeploy`), HOẶC checkout commit tốt gần nhất rồi `railway up` lại từ thư mục service.
- Đổi env var qua API: `variableUpsert` + `serviceInstanceRedeploy` là đủ (đã verify 27/07).
- Worker chết giữa chuỗi sync sáng: guard giữ-bảng-cũ bảo vệ dữ liệu (lỗi nổ TRƯỚC khối ghi, bảng giữ bản cũ); chạy lại chuỗi theo thứ tự trong `supabase-schema`, đừng chạy lẻ bước giữa.
- Theo dõi credit Railway — cảnh báo sớm cho captain khi tốc độ đốt bất thường (tiền lệ 2,98 USD/5 ngày, 18/8).

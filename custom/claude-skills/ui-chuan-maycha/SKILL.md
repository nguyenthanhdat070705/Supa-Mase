---
name: ui-chuan-maycha
description: Design system + khung SPA chuẩn của web app Demand Planning MayCha. Dùng TRƯỚC KHI build/sửa bất kỳ màn hình nào (thêm screen mới, sửa layout, style bảng/card/filter/nút), hoặc khi review giao diện trước deploy.
---

# UI chuẩn MayCha — mọi màn hình cùng một khuôn

Nguồn sự thật thiết kế (read-only): `data/dp-ui-standard-2026-08-19/design.html` + README parity contract cạnh nó. Bản `design/MayCha Demand Planning.dc.html` cũ đã bị thay thế — đừng theo.

## Design tokens (bắt buộc)

- Toàn bộ token nằm ở `app/static/ds/` — entry duy nhất là `ds/styles.css` (import tokens/{typography,colors,spacing,effects,layout,dark}.css theo thứ tự, đừng đảo).
- Hệ màu xây quanh **crimson logo `#BE1E4C`** (`--mc-crimson-500`), neutral hơi lạnh (`--mc-n-*`, search-field fill `#EDF1F5`).
- Luật 2 tầng: PRIMITIVE (`--mc-*`) cấm component tham chiếu trực tiếp — chỉ dùng SEMANTIC alias. Màn hình lane dash phụ thuộc DUY NHẤT vào biến token DS, không dựa class của `app.css`.
- Số kiểu VN: `1.234.567` (chấm ngăn ngàn), ngày `DD/MM/YYYY`, tiền không số lẻ — dùng formatter sẵn trong `dp_data` (vi-VN), cấm tự format tay mỗi nơi một kiểu.

## Khung SPA (shell / screen split, chuẩn 19/8)

- `app/static/ui/app.js` = SHELL CORE only: sidebar **13 tile cố định** (mảng `SCREENS` hard-code thứ tự/nhãn/tint/icon), topbar (Kỳ/brand/kho, Đồng bộ lại, Mobile, user menu), statusbar, boot qua `/api/bootstrap`. KHÔNG nhét logic màn hình vào shell.
- Mỗi màn = 1 lane file `app/static/ui/screens/<lane>.{js,css}` tự đăng ký `window.DP_SCREENS[key] = { render(container, ctx) }`. Key cố định: `tongquan, dp, beginstock, pr, sosanhpr, salesmix, target, tonkho, stockmin, usage, dathangnvl, bom, cuahang`.
- `ctx` = `{key, period, brand, warehouse, boot, refresh, go, toast, api, setSubtitle, setRows, setReview}`; filter toàn cục đổi → shell bắn `dp:filters-changed`, screen tự nghe mà vẽ lại.
- Backend theo lane: `app/api_{dash,plan,inv}.py` expose `router` (`/api/<group>/...`), mount qua `_mount_screen_router()` có guard (lane hỏng app vẫn boot). Thêm màn mới = thêm lane screen + route lane, đừng đụng legacy `/api/m/{page}` trừ khi bắt buộc.
- Static asset đổi là phải qua cơ chế cache-bust sẵn có (`_bust()`/`_asset_v()` gắn `?v=<mtime>`) — đừng tự chế query string.

## Sharp edges đã trả giá (cấm tái phạm)

1. **`[hidden]` bị CSS đè:** class đặt `display:flex/grid` trên element dùng attribute `hidden` sẽ VÔ HIỆU hidden → overlay tối phủ vĩnh viễn chặn mọi click (sự cố 18/8). Mọi overlay phải có `.xxx-overlay[hidden]{display:none!important}`; build content trước, show overlay sau.
2. **Test UI phải click thật:** `element.click()` trong eval là synthetic click bỏ qua hit-testing, không phát hiện lớp chặn. Kiểm thử phải dùng `elementFromPoint`/click theo tọa độ thật.
3. **Không bịa số:** KPI/chip trong design mà không có nguồn dữ liệu thật → BỎ, ghi chú parity flag; cấm fabricate.
4. Màn nặng dùng bảng precalc/prewarm (vd Sales Mix dùng `Fact_Sales_Mix_Final` monthly, không aggregate 30 ngày trên bảng thô — quá chậm trên instance nhỏ).

## Nghiệm thu giao diện

Chạy đủ checklist `dp-app-checklist` trước khi báo xong: đối chiếu số với query Supabase, 3 cỡ màn **1920/1366/375**, login/logout/deep-link, console sạch lỗi đỏ, mỗi màn load <2s, screenshot từng breakpoint gửi captain.

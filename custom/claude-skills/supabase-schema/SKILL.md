---
name: supabase-schema
description: Quy tắc thiết kế & migration database Supabase "Demand Planning TTVH" (cqpqisgighxgwpjelvxr). Dùng TRƯỚC KHI tạo/sửa bất kỳ bảng nào, viết file sql/ mới, đổi cách worker ghi bảng, hoặc khi cần thêm cột/index cho màn hình mới.
---

# Supabase schema — quy ước đã chốt, không tự chế

Nguồn sự thật: thư mục `sql/` của repo demand-planning + `worker/main.py`. Mọi quy ước dưới đây đúc từ 42 file DDL đang chạy.

## Đặt tên (luật captain 29/07 — tên cột GIỮ NGUYÊN y hệt file Excel nguồn)

- Bảng: `Fact_*` (số liệu giao dịch/tính toán), `Dim_*` (danh mục), `Cache_*` (bảng dẫn xuất cho app đọc nhanh). Schema luôn `public`, tên bảng trong ngoặc kép: `public."Fact_Sales_Mix_Final"`.
- Cột: giữ NGUYÊN tên từ nguồn — kể cả tiếng Việt có dấu (`"Mã điểm"`, `"Tổng Tiền"`) và CamelCase SAP (`"ItemCode"`, `"DocEntry"`) — nên MỌI câu SQL phải quote tên cột. CẤM "chuẩn hóa" snake_case làm lệch nguồn.
- Index: `ix_<bảng viết tắt>_<cột>` (vd `ix_sales_mix_final_diem_thang`).

## File DDL

- Mỗi bảng/thay đổi lớn = 1 file `sql/NN_ten_viec.sql`, đánh số tiếp theo số lớn nhất hiện có (đang tới 42 — chú ý đã có 2 lần trùng số 41/42, kiểm tra `ls sql/` trước khi đặt số).
- DDL phải idempotent: `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, chạy lại không lỗi.
- Mỗi bảng mới: `ALTER TABLE ... ENABLE ROW LEVEL SECURITY;` ngay sau CREATE (mẫu: `sql/30_create_fact_sales_mix_final.sql`).
- Đầu file ghi comment: bảng làm gì, captain chốt khi nào, ai ghi bảng này (worker chain / script tay nào).

## Ai được ghi gì (ranh giới cứng)

- **App (Gói 1) CHỈ ĐỌC** — ngoại lệ duy nhất: `Dim_account_Signin` (login), `Dim_App_Config` (cờ review), `Fact_PR_Draft` (phiếu PR nháp).
- **Worker sync 8g15** thay-nguyên-bảng trong 1 giao dịch (DELETE+COPY), `statement_timeout` theo `DP_SYNC_WRITE_TIMEOUT` (mặc định 120s). Chuỗi mix sáng theo đúng thứ tự: `MIX_TC_INHOUSE → MIX_TC_FRANCHISE → RABOM_TARGET_COLS → MIX_TOTAL_NVL → MIX_TOTAL_NVL_FINAL → MIX_SAFETY_STOCK → MIX_MIN_STOCK` — thêm bước mới phải đặt đúng chỗ phụ thuộc, không chen tùy tiện.
- **Guard giữ-bảng-cũ:** nguồn trống/lỗi → KHÔNG ghi đè, giữ bản cũ + báo Telegram. Mọi đường ghi mới phải giữ nguyên guard này.
- Bảng `Cache_*` rebuild nguyên khối bằng script chuyên trách (vd `scripts/refresh_caches_bomstore.py`): 1 kết nối, transaction ghi có giới hạn, nghỉ 30s giữa các pha nặng.

## Migration an toàn (dữ liệu đang chạy hằng ngày)

1. Chỉ ADDITIVE khi có thể: thêm bảng/cột/index mới thay vì sửa-tại-chỗ. `DROP TABLE`/`DROP COLUMN`/đổi tên cột = việc không đảo ngược được → phải có lệnh captain rõ ràng.
2. Trình tự đổi bảng đang được worker ghi: (a) tạo DDL mới idempotent, (b) sửa worker/script ghi, (c) chạy tay 1 lượt xác nhận, (d) đối soát row count + `MAX(ngày)` trước/sau, (e) deploy worker.
3. Đổi schema xong phải chạy lại chuỗi tính phụ thuộc (xem thứ tự trên) — bảng downstream không tự cập nhật.
4. Triết lý hệ thống: **bảng tính sẵn, app chỉ SELECT.** Màn hình mới cần số tổng hợp → thêm bảng precalc/Cache + bước build, KHÔNG viết query tính trực tiếp trên bảng thô trong app (iron rule hiệu năng: màn <2s trên instance nhỏ).
5. Kết nối qua pooler (env `SUPABASE_DB_HOST/NAME/PORT` + user/pass trong `.env` — không bao giờ commit). Bảng lớn nhất ~667k dòng (`Dim_OITW TTVH`) — thao tác nặng tuân `xu-ly-data-lon`.

## Sau mỗi thay đổi schema

- [ ] `ls sql/` không trùng số file; DDL chạy lại lần 2 không lỗi.
- [ ] RLS đã bật cho bảng mới.
- [ ] Row count > 0 và `MAX(ngày)` đúng kỳ vọng sau lượt ghi đầu.
- [ ] Ghi chú bảng mới vào comment đầu file DDL + AGENTS.md của repo nếu có sharp edge.

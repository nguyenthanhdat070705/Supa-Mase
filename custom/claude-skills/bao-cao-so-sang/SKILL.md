---
name: bao-cao-so-sang
description: Format chuẩn báo cáo số liệu MayCha Demand Planning gửi captain qua Telegram — đối soát trước, báo sau. Dùng MỖI KHI gửi số cho captain: báo cáo sáng sau worker sync 8g30, trả lời câu hỏi về doanh thu/target/NVL/tồn kho, hoặc khi captain hỏi "số đâu".
---

# Báo cáo số sáng — luật "đối soát trước, báo sau"

Lý do tồn tại: đã có sự cố captain nhận dashboard/báo cáo **không có số** ("Số đâu hết rồi???" 19/8) vì job build số chết ngầm do OOM mà không ai báo. Không được để tái diễn.

## Luật vàng

**Không gửi captain bất kỳ con số nào chưa đối soát.** Mỗi số đi kèm nguồn: bảng Supabase nào, dữ liệu đến ngày nào.

## Trình tự trước khi báo

1. **Kiểm sync:** worker 8g30 sáng chạy xong chưa? Bảng đích (vd `Fact_Sales_Mix_Final`) có dữ liệu ngày mới nhất chưa?
2. **Kiểm chất lượng tối thiểu:** row count > 0; `MAX(ngày)` = ngày kỳ vọng; không tụt đột biến số store so với hôm qua.
3. **Nếu job build chết** (OOM, lỗi SQL, parquet hỏng): báo THẲNG — "job X chết lúc Y vì Z, số chưa có, đang chạy lại, ETA T" — CẤM im lặng, CẤM gửi bảng trống, CẤM gửi số cũ giả làm số mới.

## Format tin Telegram

```
[Báo số 21/08]
• Doanh thu hôm qua: 2.145.300.000 đ (Fact_Sales_Mix_Final, 20/08)
• Bill count: 12.480 (↑3% vs 19/08)
• NVL cần đặt tuần này: 34 mã (Fact_Min_Stock_Inv)
⚠️ Tồn âm: 39/224 mã (đang chờ captain chốt chính sách)
Đối soát: OK — sync 8g30 chạy đủ, số khớp nguồn.
```

- Số kiểu VN: chấm ngăn ngàn (1.234.567), tiền không số lẻ.
- Bất thường (lệch >20% vs hôm trước, tồn âm, thiếu store, ngày dữ liệu cũ) → gắn ⚠️ + một câu giải thích hoặc "đang điều tra".
- Bảng dài/biểu đồ → xuất file Excel/ảnh gửi kèm, không dán bảng dài vào chat.
- Dòng cuối luôn là **"Đối soát: …"** — OK hay lý do chưa OK.

---
name: dp-app-checklist
description: Checklist nghiệm thu BẮT BUỘC trước khi báo "xong" bất kỳ màn hình nào của web app Demand Planning MayCha (FastAPI + Supabase + Railway). Dùng khi hoàn thành hoặc sửa màn Tổng quan, Đặt hàng, NVL, Target, Tham số, dashboard, hoặc trước khi deploy lên Railway.
---

# DP-app checklist — chưa qua đủ mục thì chưa được báo "xong"

Áp dụng cho web app Demand Planning (service `demand-app` trên Railway, đọc Supabase "Demand Planning TTVH").

## 1. Số phải đúng (quan trọng nhất)

- [ ] Mỗi con số hiển thị trên màn được đối chiếu với **query chạy tay trên Supabase** — ghi lại query + kết quả đối chiếu vào báo cáo nghiệm thu.
- [ ] Số kiểu VN: `1.234.567` (chấm ngăn ngàn), ngày `DD/MM/YYYY`, tiền không số lẻ.
- [ ] Dữ liệu hiển thị đúng ngày (không cache số cũ giả làm mới — cache TTL 5 phút là tối đa).

## 2. Nguyên tắc hệ thống

- [ ] App **CHỈ ĐỌC** Supabase (Gói 1 không ghi bất kỳ bảng nào, trừ màn Tham số khi captain duyệt riêng).
- [ ] Không lộ secret: soi source + response không thấy connection string, password, token.

## 3. Hiệu năng

- [ ] Mỗi màn load **<2 giây** (đo thật bằng đồng hồ trang, không ước lượng).
- [ ] Query nặng đã đẩy xuống bảng precalc, không tính trực tiếp trên bảng thô.

## 4. Giao diện

- [ ] Check 3 cỡ màn: **1920**, **1366**, **mobile 375** — không vỡ layout, không nuốt tiêu đề.
- [ ] Login hoạt động; logout hoạt động; vào URL sâu khi chưa login thì bị đẩy về /login.
- [ ] Console trình duyệt sạch lỗi đỏ.

## 5. Bằng chứng nghiệm thu gửi captain

- [ ] Screenshot từng breakpoint (1920/1366/375).
- [ ] Bảng đối chiếu số màn-hình ↔ Supabase.
- [ ] Nếu deploy Railway: mở domain production thật, xác nhận live rồi mới báo — CẤM báo "đã deploy" khi chưa tự mở lại trang.

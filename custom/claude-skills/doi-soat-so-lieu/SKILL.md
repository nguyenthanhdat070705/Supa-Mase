---
name: doi-soat-so-lieu
description: Quy trình đối soát số liệu tự động cho app Demand Planning — số trên màn hình phải khớp nguồn trước khi lên app hoặc báo captain. Dùng khi build/sửa màn hình có số, sau worker sync sáng, khi số bị nghi lệch, hoặc trước khi deploy bản đổi logic tính.
---

# Đối soát số liệu — số chưa khớp nguồn thì chưa được lên màn

Nối dài luật "đối soát trước, báo sau" của `bao-cao-so-sang` sang việc build app: mọi con số hiển thị phải truy được về nguồn và khớp.

## Chuỗi nguồn sự thật (một chiều, không đảo)

```
DataWarehouse (parquet/Excel, OneDrive qua MS Graph — CHỈ ĐỌC, cấm đụng)
   → worker sync 8g15 (thay-nguyên-bảng, 1 giao dịch, guard giữ-bảng-cũ)
      → bảng precalc Supabase (Fact_*/Cache_* — sự thật của app)
         → app CHỈ SELECT (không tự aggregate bảng thô)
```

Số app lệch → soi từ DƯỚI lên: query bảng precalc trước, rồi mới nghi worker, cuối cùng mới nghi nguồn DataWarehouse.

## Ngữ nghĩa số (captain đã chốt — sai là sai nghiệp vụ)

- **"Doanh thu" = cột "Tổng tiền"** (TRƯỚC giảm giá + chiết khấu). **"Thực thu"** = sau giảm. Nhãn nào lấy đúng cột đó, cấm trộn (captain chốt 6/8).
- Số lượng NVL màn Demand hiển thị theo **đơn vị mua KBMD** (`Fact_Min_Stock_Inv.ItemCode` chuẩn hóa); dòng không có quy đổi tin cậy để nguyên `—`, CẤM bịa quy đổi.
- Số kiểu VN: `1.234.567`, ngày `DD/MM/YYYY`, tiền không số lẻ.

## Đối soát khi build/sửa màn hình (bắt buộc trước khi báo xong)

1. Mỗi số trên màn ↔ **query chạy tay trên Supabase**, ghi lại query + kết quả vào báo cáo nghiệm thu (theo `dp-app-checklist`).
2. Tiền/số lượng: khớp **tuyệt đối** (lệch 1 đồng cũng là fail — thường do trộn Tổng tiền/Thực thu, thiếu filter Brand/kho, hoặc double-count combo).
3. Đổi logic tính → so bản mới vs bản cũ trên cùng kỳ dữ liệu, giải thích được từng khoản lệch rồi mới thay.
4. Kiểm thử số viết thành test trong `tests/` (mẫu: `tests/test_inv.py`, `tests/test_plan.py`, `scripts/test_worker_morning_chain.py`) để lần sau tự chạy.

## Đối soát sau worker sync sáng (trước khi báo số)

- [ ] Bảng đích chuỗi mix (`Fact_Sales_Mix_Final`, `Fact_TC_InHouse/Franchise`, `Fact_Total_NVL_Final`, `Fact_Min_Stock_Inv`) có dữ liệu ngày mới nhất: row count > 0, `MAX(ngày/tháng)` đúng kỳ vọng.
- [ ] Số store không tụt đột biến so với hôm qua; lệch >20% ngày-qua-ngày → ⚠️ + điều tra trước khi báo.
- [ ] Sync chết → báo thẳng theo luật `bao-cao-so-sang`: job nào, chết lúc nào, vì sao, ETA — cấm gửi số cũ giả làm mới.

## Kỷ luật thực thi

- Đối soát nặng chạy bằng SQL trên Supabase (GROUP BY/SUM phía server), tuân `xu-ly-data-lon` — cấm kéo bảng thô về pandas để "so cho chắc".
- App có cache TTL tối đa 5 phút — đối soát số màn hình phải chắc không nhìn số cache cũ (bấm Đồng bộ lại / đợi TTL).
- Kết quả đối soát là bằng chứng gửi kèm: bảng so sánh màn-hình ↔ Supabase, không nói suông "đã khớp".

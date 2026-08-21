---
name: xu-ly-data-lon
description: Quy tắc BẮT BUỘC khi xử lý dữ liệu lớn (parquet DataWarehouse, Excel >10MB, bảng Supabase lớn) để không bao giờ bị OOM-kill trên VM chỉ có 7.2GB RAM. Dùng TRƯỚC KHI viết bất kỳ script python/pandas nào đọc dữ liệu, khi build bảng doanh thu/mix/NVL/target, hoặc khi một job trước đó từng chết vì hết RAM.
---

# Xử lý data lớn — chống OOM

Bối cảnh: VM WSL chỉ có **7.2GB RAM** chia cho firstmate + crew + jobs. Lịch sử đã có 3 vụ OOM-kill (claude 6.8GB, python3 6.8GB ngày 6/8; python3 3GB ngày 19/8) — đều do load nguyên bảng vào pandas. File nguồn ~70MB có thể phình ×100 lần trong RAM.

## Luật cứng (không có ngoại lệ)

1. **Đẩy tính toán xuống Supabase trước tiên.** GROUP BY / JOIN / SUM chạy bằng SQL trên server, script chỉ SELECT kết quả đã gọn. Triết lý hệ thống: *bảng tính sẵn, app chỉ SELECT*. Nếu một bảng báo cáo có thể viết thành một câu SQL trên bảng precalc — PHẢI viết SQL, cấm kéo dữ liệu thô về tính bằng pandas.

2. **Parquet: đọc theo cột + theo lô.** Dùng `pyarrow.parquet.read_table(path, columns=[...], filters=[...])` hoặc `polars.scan_parquet()` (lazy/streaming). CẤM `pd.read_parquet(path)` nguyên file không chỉ định cột.

3. **Excel lớn (>10MB): stream, đừng nuốt.** `openpyxl.load_workbook(path, read_only=True)` và duyệt từng dòng, ghi ra CSV/parquet trung gian rồi xử lý tiếp theo chunk. CẤM `pd.read_excel()` nguyên workbook lớn.

4. **Ngân sách RAM mỗi job: ≤1.5GB.** Trước bước nặng chạy `free -m`; nếu available <2GB thì DỪNG và báo, không chạy liều.

5. **Chunk + xả bộ nhớ.** Xử lý theo `chunksize=50_000` dòng; giữa các chunk `del` biến lớn + `gc.collect()`; kết quả trung gian ghi ra đĩa (`/tmp`), không tích trong RAM.

6. **Không chạy song song 2 job data nặng.** Xếp hàng tuần tự.

7. **Sau một vụ OOM:** đọc `dmesg | grep -i "out of memory"` xác nhận thủ phạm, chạy lại phiên bản đã chia chunk. CẤM chạy lại y nguyên script cũ.

## Dấu hiệu phải dừng tay viết lại

- Script có `pd.read_excel(` hoặc `pd.read_parquet(` không có `columns=`
- Có `.merge()` giữa hai DataFrame đều >500k dòng (đáng lẽ JOIN bằng SQL)
- Kết quả cuối chỉ là bảng tổng hợp vài trăm dòng nhưng script kéo về hàng triệu dòng thô

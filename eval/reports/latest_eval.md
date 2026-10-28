# Báo Cáo Đánh Giá Hệ Thống RPA 2.0 (Evaluation Report)

- **Thời gian**: 2026-10-26T23:16:18.701143
- **Chế độ chạy**: `mock`
- **Tổng quan kết quả**:
  - **Intent Classification**: 95.2%
  - **Price Normalization**: 96.3% (Chu kỳ: 94.4%)
  - **Catalog Matching**: 100.0%
  - **Golden Pages Verification**: 100.0%

---

## 1. Phân loại Ý định (Intent Classification)
- Tổng test cases: 62
- Số câu phân loại đúng: 59
- Độ chính xác: **95.2%**

## 2. Chuẩn hóa Giá & Chu kỳ (Price Normalization)
- Tổng chuỗi giá: 54
- Độ chính xác giá VND: **96.3%**
- Độ chính xác chu kỳ (tháng/năm/trọn đời): **94.4%**

## 3. Đối soát & Ghép nối Danh mục (Catalog Matching)
- Tổng sản phẩm đối soát: 6
- Độ chính xác ghép SKU & ngưỡng tin cậy: **100.0%**

## 4. Bộ dữ liệu Vàng HTML (Golden Pages)
- Tổng số trang mẫu: 17
- Trạng thái trang hợp lệ: **100.0%**

---
*Báo cáo được tạo tự động bởi `eval/run_eval.py`.*
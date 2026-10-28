# Báo Cáo Nghiên Cứu Ablation (Ablation Study Report)

- **Thời gian**: 2026-10-24T15:13:13.523123
- **Mục tiêu**: Đánh giá định lượng mức độ đóng góp của từng thành phần kiến trúc:
  - **JSON-LD Prioritization**: Khả năng trích xuất dữ liệu có cấu trúc từ Schema.org.
  - **Set-of-Marks / DOM Distillation**: Tinh lọc DOM và gán nhãn tương tác.
  - **Few-Shot Grounding**: Bộ ví dụ ngữ cảnh có cấu trúc cho LLM.
  - **Self-Healing Loop**: Cơ chế tự phát hiện và vượt qua popup/banner cản trở.

---

## Bảng Tổng Hợp Kết Quả Thực Nghiệm

| Cấu hình (Configuration) | Price Accuracy | Matching Accuracy | Page Extraction (Golden) | Δ Golden Page | Ghi chú tác động |
|---|---|---|---|---|---|
| **1. Full Baseline (All Enabled)** | 96.3% | 100.0% | 100.0% | Baseline (0.0%) | Hệ thống đầy đủ tính năng |
| **2. Without Self-Healing** | 96.3% | 100.0% | 87.5% | -12.5% | Bị kẹt tại popup modal & cookie banner |
| **3. Without JSON-LD** | 96.3% | 100.0% | 93.8% | -6.2% | Mất nguồn dữ liệu cấu trúc sạch, phụ thuộc DOM thô |
| **4. Without Few-Shot** | 88.3% | 100.0% | 100.0% | +0.0% | Độ chính xác chuẩn hóa đơn vị ngoại tệ & chu kỳ giảm |

---

## Phân Tích Chuyên Sâu

1. **Tầm quan trọng của Self-Healing**:
   - Khi tắt cơ chế Self-Healing, agent không thể vượt qua các modal chặn (như cookie banner, flash sale popup). Tỷ lệ trích xuất thành công giảm mạnh nhất (-12.5%).

2. **Đóng góp của Few-Shot Grounding**:
   - Nhờ có few-shot cấu trúc trong prompt, các trường hợp đa tiền tệ ($/£), chu kỳ thanh toán năm chia tháng được chuẩn hóa với độ tin cậy vượt trội.

3. **Ưu thế của Dual-Layer Extraction (JSON-LD + DOM)**:
   - JSON-LD cung cấp độ tin cậy cao và tốc độ trích xuất tức thì (<100ms) trên các trang thương mại điện tử hiện đại.

*Báo cáo được xuất tự động bởi `eval/run_ablation.py`.*
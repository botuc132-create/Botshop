# Telegram Key Shop Bot — Railway

Source này dùng `bot.py` làm tiến trình Telegram polling worker, PostgreSQL/Supabase làm cơ sở dữ liệu.

## Deploy lên Railway

1. Tạo một GitHub repository mới và tải toàn bộ nội dung thư mục này lên (bao gồm `railway.toml`, `Procfile`, `requirements.txt` và `bot.py`).
2. Trong Railway chọn **New Project → Deploy from GitHub Repo** rồi chọn repository.
3. Mở service → **Variables** và thêm:
   - `BOT_TOKEN`: token bot lấy từ BotFather (bắt buộc). Có thể đặt tên `TELEGRAM_BOT_TOKEN` thay thế; không cần tải file `.env` lên Railway.
   - `ADMIN_IDS`: Telegram numeric ID của admin; nhiều ID cách nhau bằng dấu phẩy.
   - `DATABASE_URL`: PostgreSQL connection string. Nếu dùng Supabase trên Railway, ưu tiên Session Pooler; bật `sslmode=require`. Không đưa dấu ngoặc vuông quanh mật khẩu và URL-encode ký tự đặc biệt.
   - `LOG_LEVEL`: `INFO` (tùy chọn).
   - Các biến `QR_*` nếu muốn ghi đè cấu hình QR mặc định.
4. Deploy. Railway sẽ cài thư viện từ `requirements.txt` và chạy `python bot.py`.
5. Xem tab **Deployments → View Logs**. Khi thành công sẽ có log `Shop bot started with PostgreSQL`. Nếu log báo thiếu biến, kiểm tra đúng tên trong **Service → Variables**, rồi Deploy lại.
6. Mở Telegram, gửi `/start` và thử các chức năng người dùng/Admin.

## Lưu ý
- Đây là Telegram long-polling worker, không cần mở HTTP port hoặc tạo domain.
- Không tạo nhiều replica/service chạy cùng một bot token vì nhiều tiến trình polling có thể xung đột.
- Không tải file `.env` chứa token hoặc mật khẩu database lên GitHub.
- Nếu bot báo lỗi kết nối database, kiểm tra `DATABASE_URL`, mật khẩu, SSL và chọn đúng Session Pooler host/port.
- `railway.toml` đặt lệnh khởi động là `python bot.py`; `Procfile` cũng khai báo worker tương ứng.

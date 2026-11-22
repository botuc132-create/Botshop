# Jinny Cheat Bot + Mini App (một dịch vụ duy nhất)

Dự án chạy Telegram bot polling và Mini App/API trong **một tiến trình Python**. `bot.py` dùng chung PostgreSQL cho bot và Mini App; không cần tạo thêm web service.

## File cần có trong thư mục gốc

- `bot.py` — Telegram bot, xác thực Telegram Mini App, API và quản trị.
- `index.html` — giao diện Mini App (được `bot.py` phục vụ tại `/`).
- `requirements.txt` — thư viện Python.
- `railway.toml` / `Procfile` — cấu hình chạy `python bot.py`.

## Biến môi trường

Thiết lập trong Railway Variables hoặc file `.env` riêng trên VPS:

- `BOT_TOKEN`: token bot từ BotFather.
- `ADMIN_IDS`: Telegram ID của các admin, nhiều ID phân tách bằng dấu phẩy.
- `DATABASE_URL`: URL PostgreSQL hiện tại của dự án. Dùng đúng database cũ để giữ user, số dư, lịch sử mua và kho key.
- `MINI_APP_URL`: domain HTTPS công khai của service, ví dụ `https://ten-service.up.railway.app/`.
- `PORT`: nền tảng tự cấp; mặc định `3000` nếu chạy VPS và không khai báo.
- `QR_BANK`, `QR_ACCOUNT`, `QR_HOLDER`, `QR_TEMPLATE`, `QR_SHOWINFO`, `QR_MEMO_SUFFIX`: cấu hình QR (tùy chọn).
- `SHOPHENRY_API_TOKEN`, `SHOPHENRY_API_URL`: chỉ cần nếu có sản phẩm Aim Hack dùng ShopHenry.
- `LOG_LEVEL=INFO` (tùy chọn).
- `SEPAY_WEBHOOK_API_KEY`: khóa xác thực API Key đã tạo trong cấu hình Webhook SePay (khuyến nghị). Hoặc dùng `SEPAY_WEBHOOK_SECRET` nếu chọn HMAC-SHA256. Không cấu hình cả hai trừ khi bạn chủ động cần hỗ trợ cả hai.
- `SEPAY_AUTO_APPROVE_DELAY=6`: thời gian chờ trước khi tự duyệt (giây).

Không đưa `.env`, bot token hoặc database password lên GitHub.

## Chạy

Cài thư viện: `pip install -r requirements.txt`

Chạy duy nhất một tiến trình: `python bot.py`

`bot.py` vừa khởi chạy Telegram polling vừa mở web server tại `0.0.0.0:$PORT`. Đặt `MINI_APP_URL` bằng domain HTTPS của service, sau đó gửi `/start` cho bot và bấm **🚀 Mở Jinny Cheat Mini App**.

## Admin trong Mini App

Vào **Cá nhân → Quản trị Admin**. Danh sách chức năng được xác thực lại ở máy chủ theo `ADMIN_IDS`; việc ẩn nút trên giao diện không phải lớp bảo mật duy nhất. Có thể xem thống kê, tìm người dùng, khóa/mở khóa, cộng/trừ số dư, cấp/gỡ Sell, xem lịch sử, thêm danh mục/sản phẩm/key, cập nhật link hướng dẫn, gán ShopHenry ID, sửa giá, đổi tên/xóa danh mục/sản phẩm, xem kho, bật/tắt bảo trì, tải NewsEmoji và gửi broadcast.

## Dữ liệu và thanh toán

Mini App và Telegram bot cùng sử dụng `DATABASE_URL`. Khi cập nhật code, không đổi sang database mới hoặc xóa volume/database cũ nếu muốn giữ số dư, người dùng, lịch sử và kho key.

Tạo QR tạo đơn nạp `pending` với nội dung riêng dạng `SEVQR{user_id}NAP{deposit_id}`. Endpoint nhận giao dịch SePay là `https://<domain-Railway>/webhook/sepay`. Cấu hình Webhook SePay nhận tiền vào (`In_only`), xác thực bằng API Key hoặc HMAC-SHA256, và đặt đúng URL trên. Trong Railway Variables, đặt cùng khóa vào `SEPAY_WEBHOOK_API_KEY` (nếu dùng API Key) hoặc `SEPAY_WEBHOOK_SECRET` (nếu dùng HMAC-SHA256).

Khi webhook báo giao dịch tiền vào, bot chỉ khớp nội dung chuyển khoản và số tiền chính xác với một đơn đang `pending`/`submitted`. Sau 6 giây, bot cộng tiền và đổi trạng thái `approved` trong cùng giao dịch database. Bảng `sepay_webhook_events` dùng ID giao dịch SePay làm khóa chính để webhook retry/replay không cộng tiền hoặc ghi log lần hai. Admin vẫn nhận thông báo khi tự duyệt; nút duyệt thủ công vẫn được giữ làm phương án dự phòng.

**Lưu ý:** Webhook chỉ hoạt động nếu SePay thực sự nhận được giao dịch ngân hàng và gửi event đến webhook. QR được tạo bởi giao diện không tự đảm bảo webhook sẽ phát sinh; cần thử một giao dịch thực tế hoặc dùng chế độ test của SePay trước khi bật cho người dùng. SePay yêu cầu endpoint phản hồi JSON `{"success": true}` với HTTP 200/201. Xem tài liệu: https://developer.sepay.vn/vi/sepay-webhooks/tich-hop-webhook.

## Kiểm tra sau deploy

1. Kiểm tra `https://domain-cua-ban/health` trả về `ok: true`.
2. Gửi `/start`, mở Mini App từ nút Web App của bot.
3. Kiểm tra số dư, danh mục, lịch sử.
4. Đăng nhập bằng một Telegram ID có trong `ADMIN_IDS`, mở tab Cá nhân và thử thống kê.
5. Cấu hình SePay Webhook URL `https://<domain-Railway>/webhook/sepay`, chọn tiền vào và API Key/HMAC đúng với biến môi trường.
6. Gửi thử webhook trong SePay hoặc giao dịch nhỏ; kiểm tra log `[SEPAY] MATCHED` rồi `[SEPAY] AUTO_APPROVED`. Chỉ bật cho người dùng sau khi xác nhận giao dịch thật khớp chính xác.

Chỉ chạy **một bản polling** cho mỗi bot token. Nếu dùng VPS, mở cổng `PORT` và cấu hình reverse proxy/domain HTTPS để Telegram Mini App truy cập được.

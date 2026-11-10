# Jinny Cheat Bot + Mini App (một dịch vụ duy nhất)

Dự án dùng `python-telegram-bot 21.6`, PostgreSQL và **một tiến trình duy nhất**. File `bot.py` vừa chạy Telegram bot polling, vừa phục vụ giao diện Mini App và các endpoint nội bộ qua cùng domain/port. Không cần file `web_api.py`, không cần tạo Railway API service riêng.

## 1. Biến môi trường

Cấu hình trong Railway Variables:
- `BOT_TOKEN`: token bot từ BotFather.
- `ADMIN_IDS`: Telegram ID admin, nhiều ID phân tách bằng dấu phẩy.
- `DATABASE_URL`: URL PostgreSQL đang dùng bởi bot.
- `QR_BANK`, `QR_ACCOUNT`, `QR_HOLDER`, `QR_TEMPLATE`, `QR_SHOWINFO`: thông tin tài khoản nhận tiền.
- `MINI_APP_URL`: domain HTTPS của chính Railway service này, ví dụ `https://ten-service.up.railway.app/`.
- `PORT`: Railway tự cấp; không cần tự đặt nếu Railway đã cấp.
- `LOG_LEVEL=INFO` (tùy chọn).

## 2. Deploy một Railway service

1. Deploy repository lên Railway như một service duy nhất.
2. Start Command: `python bot.py` (đã đặt trong `railway.toml`).
3. Thêm các biến môi trường ở mục 1, đặc biệt `BOT_TOKEN`, `ADMIN_IDS`, `DATABASE_URL` và các biến `QR_*`.
4. Generate domain HTTPS cho service.
5. Sao chép domain đó, đặt làm `MINI_APP_URL` (thêm dấu `/` cuối nếu muốn), rồi redeploy/restart.
6. Mở bot, gửi `/start`; menu có nút **🚀 Mở Jinny Cheat Mini App**.

Trang HTML được phục vụ trực tiếp từ `bot.py` tại `/`. Các request đọc số dư, tạo đơn nạp và xác nhận đã chuyển tiền đi về cùng domain. Trong HTML, `API_BASE_URL` tự lấy `window.location.origin`, không cần sửa tay.

## 3. Luồng nạp tiền

1. Người dùng mở Mini App từ nút Web App của bot.
2. Mini App xác thực Telegram `initData`, đọc số dư/tổng nạp/lịch sử từ PostgreSQL.
3. Khi tạo QR, bot tạo đơn `pending`, mã chuyển khoản duy nhất dạng `NAP<TelegramID>D<DepositID>` và thời hạn 5 phút.
4. Người dùng chuyển khoản rồi bấm **ĐÃ CHUYỂN TIỀN**. Đơn chuyển sang `submitted`; bot gửi thông tin đơn cùng nút **DUYỆT / TỪ CHỐI** cho admin.
5. Admin kiểm tra tiền thực nhận rồi duyệt. Bot cộng số dư và ghi `balance_transactions`; đơn đã xử lý không thể cộng lần hai. Mini App tự làm mới số dư, tổng nạp và lịch sử mỗi 15 giây.

## 4. Hết hạn và an toàn

- Máy chủ đánh dấu đơn `pending` hết hạn sau 5 phút.
- Nút “Đã chuyển tiền” không tự cộng tiền; nó chỉ gửi yêu cầu để admin kiểm tra.
- Admin có thể duyệt đơn `expired` chỉ khi đã kiểm tra sao kê và xác nhận tiền thực nhận.
- `BOT_TOKEN`, `DATABASE_URL` và thông tin ngân hàng chỉ đặt trong Railway Variables hoặc `.env` riêng, không commit lên GitHub.
- Chỉ chạy một bản bot polling cho mỗi bot token.

## 5. Kiểm thử

1. Gửi `/start` để tài khoản được tạo trong database.
2. Mở Mini App từ bot, tạo đơn nạp tối thiểu 2.000đ.
3. Bấm “Đã chuyển tiền” và kiểm tra admin nhận được thông báo.
4. Admin xác nhận tiền trong ngân hàng rồi bấm DUYỆT. QR được tạo tự động, nhưng bản source hiện tại chưa có webhook/API đối soát ngân hàng nên bước cộng tiền vẫn cần Admin duyệt thủ công.
5. Kiểm tra số dư, tổng nạp và lịch sử trong Mini App.
6. Thử duyệt lại cùng đơn: bot phải báo đã xử lý, không cộng lần hai.

**Lưu ý:** không có webhook đối soát ngân hàng trong source này. Muốn tự động duyệt mà không cần admin phải tích hợp API/webhook chính thức của ngân hàng hoặc nhà cung cấp thanh toán.

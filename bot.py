import os
import re
import asyncio
import logging
import json
import urllib.request
import urllib.error
import time
import hmac
import hashlib
import threading
from urllib.parse import parse_qsl
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from html import escape
from pathlib import Path
from urllib.parse import urlencode
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import uvicorn
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env')

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters,
)

DATABASE_URL = os.getenv('DATABASE_URL', '').strip()
BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()
SHOPHENRY_API_TOKEN = os.getenv('SHOPHENRY_API_TOKEN', '').strip()
SHOPHENRY_API_URL = os.getenv('SHOPHENRY_API_URL', 'https://shophenry.io.vn/api/seller.php').strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip().isdigit()}
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
QR_BANK = os.getenv('QR_BANK', 'BIDV').strip()
QR_ACCOUNT = os.getenv('QR_ACCOUNT', '8817735872').strip()
QR_HOLDER = os.getenv('QR_HOLDER', 'LE MINH DUONG').strip()
QR_TEMPLATE = os.getenv('QR_TEMPLATE', 'compact2').strip()
MINI_APP_URL = os.getenv('MINI_APP_URL', '').strip()
QR_MEMO_PREFIX = 'NAP'
DB_POOL = None

logging.basicConfig(format='%(asctime)s | %(levelname)s | %(name)s | %(message)s', level=LOG_LEVEL)
log = logging.getLogger('key_shop_bot')

USER_KB = ReplyKeyboardMarkup([[KeyboardButton('↩️ Trở về')]], resize_keyboard=True, is_persistent=True)

ADMIN_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton('📢 Thông báo', callback_data='adm:broadcast')],
    [InlineKeyboardButton('📊 Thống kê hệ thống', callback_data='adm:stats')],
    [InlineKeyboardButton('👤 Thông tin người dùng', callback_data='adm:usercheck')],
    [InlineKeyboardButton('➕ Thêm sản phẩm', callback_data='adm:addproduct')],
    [InlineKeyboardButton('🔗 Thêm link cách cài', callback_data='adm:addguide')],
    [InlineKeyboardButton('🔑 Thêm key sản phẩm', callback_data='adm:addkeys')],
    [InlineKeyboardButton('📦 Kiểm tra kho', callback_data='adm:stock')],
    [InlineKeyboardButton('🛠 Bảo trì: Bật / Tắt', callback_data='adm:maintenance')],
])

class Database:
    def __init__(self):
        if DB_POOL is None:
            raise RuntimeError('Database pool chưa được khởi tạo')
        self.conn = DB_POOL.getconn()
    def execute(self, query, params=None):
        return self.conn.execute(query.replace('?', '%s'), params)
    def commit(self): self.conn.commit()
    def rollback(self): self.conn.rollback()
    def close(self):
        if self.conn is not None:
            try:
                self.conn.rollback()
            except Exception:
                pass
            DB_POOL.putconn(self.conn)
            self.conn = None
    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb):
        if self.conn is not None:
            try:
                if exc_type is None:
                    self.conn.commit()
                else:
                    self.conn.rollback()
            finally:
                DB_POOL.putconn(self.conn)
                self.conn = None
        return False

def db():
    if not DATABASE_URL:
        raise RuntimeError('Thiếu DATABASE_URL trong .env')
    return Database()

def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def make_qr_url(amount, memo):
    return f"https://img.vietqr.io/image/{QR_BANK}-{QR_ACCOUNT}-{QR_TEMPLATE}.png?amount={amount}&addInfo={memo}&accountName={urllib.parse.quote(QR_HOLDER)}"

def is_admin(update): return bool(update.effective_user and update.effective_user.id in ADMIN_IDS)
def is_aim_hack_product(product_name):
    normalized = re.sub(r'[^a-z0-9]+', '', str(product_name or '').lower())
    return 'aimhack' in normalized

def init_db():
    global DB_POOL
    if DB_POOL is None:
        DB_POOL = ConnectionPool(
            conninfo=DATABASE_URL, min_size=1, max_size=20, timeout=15,
            kwargs={'row_factory': dict_row}, open=True
        )
        DB_POOL.wait(timeout=15)
    statements = [
        """CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY, username TEXT, full_name TEXT NOT NULL DEFAULT '',
            balance BIGINT NOT NULL DEFAULT 0, is_locked BOOLEAN NOT NULL DEFAULT FALSE,
            joined_at TEXT NOT NULL, last_seen TEXT NOT NULL)""",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_deposit_request_at TIMESTAMPTZ",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_seller BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE product_options ADD COLUMN IF NOT EXISTS shophenry_product_id TEXT",
        "ALTER TABLE product_options ADD COLUMN IF NOT EXISTS sell_price BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE purchases ADD COLUMN IF NOT EXISTS supplier_order_code TEXT",
        "ALTER TABLE categories ADD COLUMN IF NOT EXISTS guide_url TEXT",
        "CREATE TABLE IF NOT EXISTS bot_settings (setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL, updated_at TEXT NOT NULL)",
        "INSERT INTO bot_settings(setting_key,setting_value,updated_at) VALUES('maintenance','off',NOW()::text) ON CONFLICT(setting_key) DO NOTHING",
        """CREATE TABLE IF NOT EXISTS categories (
            id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_by BIGINT, created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY, category_id BIGINT REFERENCES categories(id) ON DELETE CASCADE,
            name TEXT NOT NULL, created_by BIGINT, created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS product_keys (
            id BIGSERIAL PRIMARY KEY, option_id BIGINT NOT NULL REFERENCES product_options(id) ON DELETE CASCADE,
            key_value TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'available' CHECK(status IN ('available','sold')),
            added_by BIGINT, added_at TEXT NOT NULL, sold_to BIGINT, sold_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS purchases (
            id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(user_id),
            category_name TEXT NOT NULL, product_name TEXT NOT NULL, duration_label TEXT NOT NULL,
            quantity INTEGER NOT NULL, unit_price BIGINT NOT NULL, total_price BIGINT NOT NULL,
            keys_text TEXT NOT NULL, created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS deposits (
            id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(user_id),
            amount BIGINT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', note TEXT, created_at TEXT NOT NULL,
            expires_at TIMESTAMPTZ)""",
    ]
    with db() as con:
        for index, sql in enumerate(statements, 1):
            try:
                con.execute(sql)
            except Exception:
                pass

def user_menu_text(user, balance):
    name = escape(user.full_name or 'Khách hàng')
    return (f'🎉 Xin chào, {name}!\n🛍 CHÀO MỪNG BẠN ĐẾN VỚI JINNY CHEAT STORE\n\n'
            f'━━━━━━━━━━━━━━━━━━\n👤 Tên: {name}\n🆔 ID: <code>{user.id}</code>\n'
            f'💰 Số dư: {balance:,}đ\n━━━━━━━━━━━━━━━━━━\n\n'
            f'👇 Chọn chức năng bên dưới:')

def main_menu_markup():
    rows = [
        [InlineKeyboardButton('👤 Tài Khoản', callback_data='menu:account'), InlineKeyboardButton('📜 Lịch Sử Mua', callback_data='menu:history')],
        [InlineKeyboardButton('🛍 Cửa Hàng', callback_data='shop:home'), InlineKeyboardButton('💳 Nạp Tiền', callback_data='menu:deposit')],
    ]
    if MINI_APP_URL:
        rows.append([InlineKeyboardButton('🚀 Mở Jinny Cheat Mini App', web_app=WebAppInfo(url=MINI_APP_URL))])
    rows.extend([
        [InlineKeyboardButton('📖 Hướng dẫn cách cài', callback_data='menu:guide')],
        [InlineKeyboardButton('🆘 Hỗ Trợ', callback_data='menu:support')],
    ])
    return InlineKeyboardMarkup(rows)

def inline_back(target='shop:home'): return InlineKeyboardMarkup([[InlineKeyboardButton('🌐 Quay lại', callback_data=target)]])

def get_categories():
    with db() as con: return con.execute('SELECT id,name,guide_url FROM categories ORDER BY id').fetchall()

def stock_for_option(option_id):
    with db() as con: return con.execute("SELECT COUNT(*) AS n FROM product_keys WHERE option_id=? AND status='available'", (option_id,)).fetchone()['n']

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None: return
    context.user_data.clear()
    stamp = now()
    with db() as con:
        con.execute("""INSERT INTO users(user_id,username,full_name,joined_at,last_seen) VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username,full_name=EXCLUDED.full_name,last_seen=EXCLUDED.last_seen""",
            (user.id, user.username or '', user.full_name or '', stamp, stamp))
        row = con.execute('SELECT balance,is_locked FROM users WHERE user_id=?', (user.id,)).fetchone()
    balance = row['balance'] if row else 0
    await message.reply_text(user_menu_text(user, balance), reply_markup=main_menu_markup(), parse_mode='HTML')

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    await update.message.reply_text("🛠 BẢNG QUẢN TRỊ ADMIN", reply_markup=ADMIN_KB)

async def show_shop(q, edit=False):
    cats = get_categories()
    txt = '🎮 DANH MỤC: SHOP\n\n❝ Chọn danh mục bên dưới để xem sản phẩm và gói thời hạn: ❞'
    if not cats:
        txt = '🛍 CỬA HÀNG\n\nHiện chưa có danh mục sản phẩm.'
        rows = [[InlineKeyboardButton('🌐 Quay Lại', callback_data='shop:home')]]
    else:
        rows = [[InlineKeyboardButton(c['name'], callback_data=f"shop:cat:{c['id']}")] for c in cats]
        rows.append([InlineKeyboardButton('🌐 Quay Lại', callback_data='shop:home')])
    
    markup = InlineKeyboardMarkup(rows)
    try:
        if edit: await q.edit_message_text(txt, reply_markup=markup)
        else: await q.message.reply_text(txt, reply_markup=markup)
    except BadRequest:
        pass

async def show_category(q, category_id, user_id):
    with db() as con:
        cat = con.execute('SELECT * FROM categories WHERE id=?', (category_id,)).fetchone()
        prods = con.execute('SELECT id, name FROM products WHERE category_id::text=?::text', (category_id,)).fetchall()
        user_row = con.execute('SELECT is_seller FROM users WHERE user_id=?', (user_id,)).fetchone()
        is_seller = bool(user_row and user_row['is_seller'])
        prod_ids = [p['id'] for p in prods]
        opts = []
        if prod_ids:
            placeholders = ','.join(['?'] * len(prod_ids))
            opts = con.execute(f"SELECT po.*, p.name AS product_name FROM product_options po JOIN products p ON p.id::text=po.product_id::text WHERE po.product_id::text IN ({placeholders}) ORDER BY po.id", [str(pid) for pid in prod_ids]).fetchall()
            
    if not cat: 
        try: await q.edit_message_text('Danh mục không còn tồn tại.')
        except BadRequest: pass
        return
    rows = []
    for o in opts:
        is_sh = is_aim_hack_product(o['product_name'])
        src_str = ('ShopHenry' if o.get('shophenry_product_id') else 'Chưa gán ID') if is_sh else f"Kho: {stock_for_option(o['id'])}"
        current_price = o['sell_price'] if (is_seller and o['sell_price'] > 0) else o['price']
        btn_text = f"✨ {o['product_name']} ({o['duration_label']}) – {current_price:,}đ ({src_str})"
        rows.append([InlineKeyboardButton(btn_text, callback_data=f"shop:option:{o['id']}")])
    rows.append([InlineKeyboardButton('🌐 Quay Lại', callback_data='shop:home')])
    
    txt = f"✨ {escape(cat['name'])}\n\n❝ Chọn gói sản phẩm bên dưới để mua: ❞"
    try:
        await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(rows), parse_mode='HTML')
    except BadRequest:
        pass

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query: return
    try:
        await query.answer()
    except Exception:
        pass
    data = query.data
    user_id = query.from_user.id

    if data == 'shop:home':
        with db() as con:
            row = con.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()
        balance = row['balance'] if row else 0
        try:
            await query.edit_message_text(user_menu_text(query.from_user, balance), reply_markup=main_menu_markup(), parse_mode='HTML')
        except BadRequest:
            pass

    elif data == 'shop:home_list' or data == 'shop:home_menu':
        await show_shop(query, edit=True)

    elif data.startswith('shop:cat:'):
        cat_id = data.split(':')[2]
        await show_category(query, cat_id, user_id)

    elif data == 'menu:account':
        with db() as con:
            row = con.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()
        balance = row['balance'] if row else 0
        try:
            await query.edit_message_text(f"👤 **TÀI KHOẢN**\n\nID: <code>{user_id}</code>\nSố dư: <b>{balance:,}đ</b>", parse_mode='HTML', reply_markup=inline_back('shop:home'))
        except BadRequest:
            pass

    elif data == 'menu:history':
        with db() as con: 
            rows = con.execute('SELECT * FROM purchases WHERE user_id=? ORDER BY id DESC LIMIT 10', (user_id,)).fetchall()
        if not rows:
            try: await query.edit_message_text('📜 Bạn chưa có lịch sử mua hàng.', reply_markup=inline_back('shop:home'))
            except BadRequest: pass
            return
        out = ['📜 **LỊCH SỬ MUA HÀNG**']
        for r in rows: 
            out.append(f"\n• Đơn #{r['id']}: {r['product_name']} ({r['duration_label']}) - {r['total_price']:,}đ")
        try:
            await query.edit_message_text('\n'.join(out), parse_mode='MARKDOWN', reply_markup=inline_back('shop:home'))
        except BadRequest:
            pass

    elif data == 'menu:deposit':
        context.user_data['waiting_deposit_amount'] = True
        try:
            await query.edit_message_text(
                "💳 **NẠP TIỀN TỰ ĐỘNG QUA QR**\n\n"
                "Vui lòng nhập số tiền bạn muốn nạp (ví dụ: `50000`):",
                parse_mode='MARKDOWN',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Hủy', callback_data='shop:home')]])
            )
        except BadRequest:
            pass

    elif data.startswith('dep:confirm:'):
        dep_id = data.split(':')[2]
        with db() as con:
            dep = con.execute("SELECT * FROM deposits WHERE id=?", (dep_id,)).fetchone()
        if dep:
            msg_admin = f"🔔 **YÊU CẦU XÁC NHẬN NẠP TIỀN TỪ BOT**\nID: <code>{user_id}</code>\nSố tiền: <b>{dep['amount']:,}đ</b>\nNội dung: <code>{dep['note']}</code>"
            for aid in ADMIN_IDS:
                try:
                    req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=json.dumps({"chat_id": aid, "text": msg_admin, "parse_mode": "HTML"}).encode('utf-8'), headers={'Content-Type': 'application/json'}, method='POST')
                    urllib.request.urlopen(req, timeout=10)
                except Exception: pass
            try:
                await query.edit_message_text("✅ Đã gửi yêu cầu xác nhận tới Admin thành công! Vui lòng chờ kiểm tra.", reply_markup=inline_back('shop:home'))
            except BadRequest:
                pass
        else:
            try: await query.edit_message_text("❌ Không tìm thấy giao dịch.", reply_markup=inline_back('shop:home'))
            except BadRequest: pass

    elif data == 'menu:guide':
        with db() as con:
            cats = con.execute("SELECT name, guide_url FROM categories WHERE guide_url IS NOT NULL AND guide_url != ''").fetchall()
        txt = "📖 **HƯỚNG DẪN CÀI ĐẶT**\n\n" + ("\n".join([f"• {c['name']}: {c['guide_url']}" for c in cats]) if cats else "Chưa có hướng dẫn.")
        try: await query.edit_message_text(txt, parse_mode='MARKDOWN', reply_markup=inline_back('shop:home'))
        except BadRequest: pass

    elif data == 'menu:support':
        try: await query.edit_message_text("🆘 **HỖ TRỢ**\nLiên hệ Admin để được giải đáp thắc mắc.", parse_mode='MARKDOWN', reply_markup=inline_back('shop:home'))
        except BadRequest: pass

    elif data.startswith('adm:'):
        if not is_admin(update): return
        action = data.split(':')[1]
        if action == 'stats':
            with db() as con:
                uc = con.execute("SELECT COUNT(*) AS n FROM users").fetchone()['n']
                rev = con.execute("SELECT SUM(total_price) AS total FROM purchases").fetchone()['total'] or 0
            try: await query.edit_message_text(f"📊 **THỐNG KÊ**\nThành viên: {uc}\nDoanh thu: {rev:,}đ", parse_mode='MARKDOWN', reply_markup=inline_back('shop:home'))
            except BadRequest: pass
        else:
            try: await query.edit_message_text("🛠 Tính năng admin quản lý qua bot.", reply_markup=inline_back('shop:home'))
            except BadRequest: pass

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if not message or not message.text: return

    if context.user_data.get('waiting_deposit_amount'):
        context.user_data['waiting_deposit_amount'] = False
        text = message.text.strip().replace(',', '').replace('.', '')
        if not text.isdigit() or int(text) < 2000:
            await message.reply_text("⚠️ Số tiền không hợp lệ! Vui lòng nhập tối thiểu từ 2.000đ trở lên.", reply_markup=USER_KB)
            return
        amount = int(text)
        memo = f"{QR_MEMO_PREFIX}{user.id}"
        qr_url = make_qr_url(amount, memo)
        stamp = now()
        with db() as con:
            cur = con.execute("INSERT INTO deposits(user_id, amount, status, note, created_at) VALUES(?, ?, 'pending', ?, ?) RETURNING id", (user.id, amount, memo, stamp))
            dep_id = cur.fetchone()["id"]
            con.commit()

        caption = (
            f"💳 **THANH TOÁN MÃ QR**\n\n"
            f"💰 Số tiền: <b>{amount:,}đ</b>\n"
            f"📝 Nội dung CK: <code>{memo}</code>\n\n"
            f"👉 Quét mã QR bên trên để thanh toán. Sau khi chuyển khoản xong, bấm nút **Xác Minh** bên dưới để báo Admin."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('✅ Đã Chuyển Tiền (Xác Minh)', callback_data=f'dep:confirm:{dep_id}')],
            [InlineKeyboardButton('❌ Hủy', callback_data='shop:home')]
        ])
        await message.reply_photo(photo=qr_url, caption=caption, parse_mode='HTML', reply_markup=kb)

# ==========================================
# CẤU HÌNH FASTAPI & MINI APP API
# ==========================================
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def verify_telegram_init_data(init_data: str) -> dict:
    try:
        parsed = dict(parse_qsl(init_data))
        hash_check = parsed.pop('hash', None)
        if not hash_check: return {}
        data_check_string = '\n'.join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest() == hash_check:
            return json.loads(parsed.get('user', '{}'))
    except Exception:
        pass
    return {}

@app.get("/")
def read_root():
    return FileResponse(BASE_DIR / "index.html")

@app.get("/api/me")
async def api_get_me(x_telegram_init_data: str = Header(None)):
    user = verify_telegram_init_data(x_telegram_init_data or "")
    if not user or "id" not in user: raise HTTPException(status_code=401)
    uid = user["id"]
    with db() as con:
        balance = con.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()['balance'] or 0
        deposits = con.execute("SELECT id, amount, status, note, created_at FROM deposits WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,)).fetchall()
        total_dep = con.execute("SELECT SUM(amount) AS total FROM deposits WHERE user_id=? AND status='approved'", (uid,)).fetchone()['total'] or 0
    return {"balance": balance, "total_deposit": total_dep, "deposits": [dict(d) for d in deposits]}

class DepositRequest(BaseModel):
    amount: int

@app.post("/api/deposits")
async def api_create_deposit(req: DepositRequest, x_telegram_init_data: str = Header(None)):
    user = verify_telegram_init_data(x_telegram_init_data or "")
    if not user or "id" not in user: raise HTTPException(status_code=401)
    if req.amount < 2000: raise HTTPException(status_code=400)
    uid = user["id"]
    memo = f"{QR_MEMO_PREFIX}{uid}"
    stamp = now()
    with db() as con:
        cur = con.execute("INSERT INTO deposits(user_id, amount, status, note, created_at) VALUES(?, ?, 'pending', ?, ?) RETURNING id", (uid, req.amount, memo, stamp))
        deposit_id = cur.fetchone()["id"]
        con.commit()
    return {"deposit_id": deposit_id, "amount": req.amount, "transfer_content": memo, "qr_url": make_qr_url(req.amount, memo), "expires_in": 300}

@app.post("/api/deposits/{deposit_id}/submitted")
async def api_deposit_submitted(deposit_id: int, x_telegram_init_data: str = Header(None)):
    user = verify_telegram_init_data(x_telegram_init_data or "")
    if not user or "id" not in user: raise HTTPException(status_code=401)
    uid = user["id"]
    full_name = user.get("first_name", "") + " " + user.get("last_name", "")
    with db() as con:
        dep = con.execute("SELECT * FROM deposits WHERE id=? AND user_id=?", (deposit_id, uid)).fetchone()
        if not dep: raise HTTPException(status_code=404)
    msg = f"🔔 **YÊU CẦU NẠP TIỀN TỪ MINI APP**\nID: <code>{uid}</code>\nTên: {escape(full_name)}\nSố tiền: <b>{dep['amount']:,}đ</b>\nMã: <code>{dep['note']}</code>"
    for aid in ADMIN_IDS:
        try:
            req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=json.dumps({"chat_id": aid, "text": msg, "parse_mode": "HTML"}).encode('utf-8'), headers={'Content-Type': 'application/json'}, method='POST')
            urllib.request.urlopen(req, timeout=10)
        except Exception: pass
    return {"status": "success"}

async def main():
    init_db()
    threading.Thread(target=lambda: uvicorn.run(app, host="0.0.0.0", port=8080), daemon=True).start()
    
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_cmd))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    
    await asyncio.Event().wait()

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): pass

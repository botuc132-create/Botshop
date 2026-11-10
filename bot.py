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
import json as _json
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
from pydantic import BaseModel, Field
import uvicorn
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env')

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, MessageEntity, WebAppInfo
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
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
QR_SHOWINFO = os.getenv('QR_SHOWINFO', 'false').strip().lower()
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
    [InlineKeyboardButton('🔗 Gán ID ShopHenry (Aim Hack)', callback_data='adm:mapshop')],
    [InlineKeyboardButton('✏️ Sửa giá / thời hạn', callback_data='adm:editproduct')],
    [InlineKeyboardButton('💰 Sửa giá Sell', callback_data='adm:editsellprice')],
    [InlineKeyboardButton('📋 Chi tiết sản phẩm', callback_data='adm:detailproduct')],
    [InlineKeyboardButton('📝 Đổi tên danh mục ngoài', callback_data='adm:renamecat')],
    [InlineKeyboardButton('🗑 Xóa sản phẩm', callback_data='adm:delproduct')],
    [InlineKeyboardButton('🔑 Thêm key sản phẩm', callback_data='adm:addkeys')],
    [InlineKeyboardButton('📦 Kiểm tra kho', callback_data='adm:stock')],
    [InlineKeyboardButton('🛠 Bảo trì: Bật / Tắt', callback_data='adm:maintenance')],
    [InlineKeyboardButton('✨ Kho Emoji NewsEmoji', callback_data='adm:emoji')],
])

DURATIONS = {'1h': '1 giờ', '1d': '1 ngày', '7d': '7 ngày', '30d': '30 ngày'}

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
def now_vn(): return datetime.now(ZoneInfo('Asia/Ho_Chi_Minh')).strftime('%d/%m/%Y %H:%M:%S')

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
        r"""DO $$ BEGIN
            IF to_regclass('public.product_options') IS NULL THEN
                EXECUTE format(
                    'CREATE TABLE public.product_options (
                        id BIGSERIAL PRIMARY KEY,
                        product_id %s NOT NULL REFERENCES public.products(id) ON DELETE CASCADE,
                        duration TEXT NOT NULL CHECK(duration IN (''1h'',''1d'')),
                        duration_label TEXT NOT NULL,
                        price BIGINT NOT NULL DEFAULT 0,
                        sell_price BIGINT NOT NULL DEFAULT 0,
                        UNIQUE(product_id, duration))',
                    (SELECT format_type(a.atttypid, a.atttypmod)
                     FROM pg_attribute a
                     WHERE a.attrelid = 'public.products'::regclass
                       AND a.attname = 'id' AND NOT a.attisdropped)
                );
            END IF;
        END $$""",
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

def admin_menu_text(): return '🛠 QUẢN TRỊ SHOP\nChọn chức năng bên dưới:'

def maintenance_enabled():
    with db() as con:
        row=con.execute("SELECT setting_value FROM bot_settings WHERE setting_key='maintenance'").fetchone()
    return bool(row and row['setting_value']=='on')

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
    try:
        with db() as con:
            con.execute("""INSERT INTO users(user_id,username,full_name,joined_at,last_seen) VALUES(?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username,full_name=EXCLUDED.full_name,last_seen=EXCLUDED.last_seen""",
                (user.id, user.username or '', user.full_name or '', stamp, stamp))
            row = con.execute('SELECT balance,is_locked FROM users WHERE user_id=?', (user.id,)).fetchone()
        if row and row['is_locked']:
            await message.reply_text('⛔ Tài khoản của bạn đang bị khóa.', reply_markup=USER_KB)
            return
        if maintenance_enabled() and user.id not in ADMIN_IDS:
            await message.reply_text('🛠 Bot đang bảo trì. Vui lòng quay lại sau.', reply_markup=USER_KB)
            return
        balance = row['balance'] if row else 0
        await message.reply_text(user_menu_text(user, balance), reply_markup=main_menu_markup(), parse_mode='HTML')
    except Exception:
        log.exception('Failed to handle /start')
        await message.reply_text('⚠️ Lỗi cơ sở dữ liệu khi khởi động.')

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text('⛔ Không có quyền admin.', reply_markup=USER_KB)
        return
    context.user_data.clear()
    await update.message.reply_text(admin_menu_text(), reply_markup=ADMIN_KB)

# Hiển thị Cửa Hàng & Danh mục sản phẩm
async def show_shop(message, edit=False):
    cats = get_categories()
    if not cats:
        txt = '🛍 CỬA HÀNG\n\nHiện chưa có danh mục sản phẩm.'
        if edit: await message.edit_text(txt, reply_markup=inline_back('shop:home'))
        else: await message.reply_text(txt)
        return
    rows = [[InlineKeyboardButton(c['name'], callback_data=f"shop:cat:{c['id']}")] for c in cats]
    rows.append([InlineKeyboardButton('🌐 Quay Lại', callback_data='shop:home')])
    txt = '🎮 DANH MỤC: SHOP\n\n❝ Chọn danh mục bên dưới để xem sản phẩm và gói thời hạn: ❞'
    if edit: await message.edit_text(txt, reply_markup=InlineKeyboardMarkup(rows))
    else: await message.reply_text(txt, reply_markup=InlineKeyboardMarkup(rows))

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
            
    if not cat: await q.edit_message_text('Danh mục không còn tồn tại.'); return
    rows = []
    for o in opts:
        is_sh = is_aim_hack_product(o['product_name'])
        if is_sh:
            src_str = 'ShopHenry' if o.get('shophenry_product_id') else 'Chưa gán ID'
        else:
            src_str = f"Kho: {stock_for_option(o['id'])}"
            
        current_price = o['sell_price'] if (is_seller and o['sell_price'] > 0) else o['price']
        btn_text = f"✨ {o['product_name']} ({o['duration_label']}) – {current_price:,}đ ({src_str})"
        rows.append([InlineKeyboardButton(btn_text, callback_data=f"shop:option:{o['id']}")])
    rows.append([InlineKeyboardButton('🌐 Quay Lại', callback_data='shop:home')])
    
    txt = f"✨ {escape(cat['name'])}\n\n❝ Chọn gói sản phẩm bên dưới để xem chi tiết và mua: ❞"
    await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(rows), parse_mode='HTML')

async def user_history(q, uid):
    with db() as con: 
        rows = con.execute('SELECT * FROM purchases WHERE user_id=? ORDER BY id DESC LIMIT 20', (uid,)).fetchall()
    if not rows:
        await q.edit_message_text('📜 Bạn chưa có lịch sử mua hàng.', reply_markup=inline_back('shop:home'))
        return
    out = ['📜 LỊCH SỬ MUA HÀNG']
    for r in rows: 
        out.append(f"\n🧾 Đơn #{r['id']} · {r['created_at']}\n{r['product_name']} · {r['duration_label']}\nSố lượng: {r['quantity']} · {r['total_price']:,}đ")
    await q.edit_message_text('\n'.join(out), reply_markup=inline_back('menu:home'))

# Xử lý tất cả các nút bấm menu và admin
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query: return
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == 'shop:home':
        with db() as con:
            row = con.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()
        balance = row['balance'] if row else 0
        await query.edit_message_text(user_menu_text(query.from_user, balance), reply_markup=main_menu_markup(), parse_mode='HTML')

    elif data == 'shop:home_list' or data == 'shop:home':
        await show_shop(query.message, edit=True)

    elif data.startswith('shop:cat:'):
        cat_id = data.split(':')[2]
        await show_category(query, cat_id, user_id)

    elif data == 'menu:account':
        with db() as con:
            row = con.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()
        balance = row['balance'] if row else 0
        await query.edit_message_text(
            f"👤 **THÔNG TIN TÀI KHOẢN**\n\n🆔 ID: <code>{user_id}</code>\n💰 Số dư: <b>{balance:,}đ</b>",
            parse_mode='HTML',
            reply_markup=inline_back('shop:home')
        )

    elif data == 'menu:history':
        await user_history(query, user_id)

    elif data == 'menu:deposit':
        await query.edit_message_text(
            "💳 **NẠP TIỀN VÀO TÀI KHOẢN**\n\nBạn có thể nạp tiền tự động cực kỳ nhanh chóng bằng cách mở **Jinny Cheat Mini App** ngay bên dưới menu chính hoặc nhắn Admin.",
            parse_mode='MARKDOWN',
            reply_markup=inline_back('shop:home')
        )

    elif data == 'menu:guide':
        with db() as con:
            cats = con.execute("SELECT name, guide_url FROM categories WHERE guide_url IS NOT NULL AND guide_url != ''").fetchall()
        if not cats:
            txt = "📖 **HƯỚNG DẪN CÀI ĐẶT**\n\nChưa có link hướng dẫn nào được cập nhật."
        else:
            txt = "📖 **HƯỚNG DẪN CÀI ĐẶT**\n\n" + "\n".join([f"• {c['name']}: {c['guide_url']}" for c in cats])
        await query.edit_message_text(txt, parse_mode='MARKDOWN', reply_markup=inline_back('shop:home'))

    elif data == 'menu:support':
        await query.edit_message_text(
            "🆘 **HỖ TRỢ KHÁCH HÀNG**\n\nLiên hệ trực tiếp Admin để được hỗ trợ xử lý các vấn đề về tài khoản và key.",
            parse_mode='MARKDOWN',
            reply_markup=inline_back('shop:home')
        )

    elif data.startswith('adm:'):
        if not is_admin(update): return
        action = data.split(':')[1]
        if action == 'stats':
            with db() as con:
                users_count = con.execute("SELECT COUNT(*) AS n FROM users").fetchone()['n']
                total_rev = con.execute("SELECT SUM(total_price) AS total FROM purchases").fetchone()['total'] or 0
            await query.edit_message_text(f"📊 **THỐNG KÊ HỆ THỐNG**\n\n👥 Tổng thành viên: {users_count}\n💰 Tổng doanh thu: {total_rev:,}đ", parse_mode='MARKDOWN', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🌐 Quay lại', callback_data='shop:home')]]))
        else:
            await query.edit_message_text(f"🛠 Chức năng quản trị [{action}] đang được thực hiện qua Bot. Bạn có thể dùng các lệnh quản lý trực tiếp.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🌐 Quay lại', callback_data='shop:home')]]))

# ==========================================
# CẤU HÌNH FASTAPI & MINI APP API
# ==========================================
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def verify_telegram_init_data(init_data: str) -> dict:
    try:
        parsed = dict(parse_qsl(init_data))
        hash_check = parsed.pop('hash', None)
        if not hash_check: return {}
        data_check_string = '\n'.join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if calculated_hash == hash_check:
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
    if not user or "id" not in user:
        raise HTTPException(status_code=401, detail="Xác thực Telegram thất bại.")
    uid = user["id"]
    with db() as con:
        u_row = con.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
        balance = u_row['balance'] if u_row else 0
        deposits = con.execute("SELECT id, amount, status, note, created_at FROM deposits WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,)).fetchall()
        total_dep = con.execute("SELECT SUM(amount) AS total FROM deposits WHERE user_id=? AND status='approved'", (uid,)).fetchone()['total'] or 0
    return {
        "balance": balance,
        "total_deposit": total_dep,
        "deposits": [dict(d) for d in deposits]
    }

class DepositRequest(BaseModel):
    amount: int

@app.post("/api/deposits")
async def api_create_deposit(req: DepositRequest, x_telegram_init_data: str = Header(None)):
    user = verify_telegram_init_data(x_telegram_init_data or "")
    if not user or "id" not in user:
        raise HTTPException(status_code=401, detail="Xác thực Telegram thất bại.")
    if req.amount < 2000:
        raise HTTPException(status_code=400, detail="Số tiền tối thiểu là 2.000đ.")
    uid = user["id"]
    memo = f"{QR_MEMO_PREFIX}{uid}"
    qr_url = make_qr_url(req.amount, memo)
    stamp = now()
    with db() as con:
        cur = con.execute(
            "INSERT INTO deposits(user_id, amount, status, note, created_at) VALUES(?, ?, 'pending', ?, ?) RETURNING id",
            (uid, req.amount, memo, stamp)
        )
        deposit_id = cur.fetchone()["id"]
        con.commit()
    return {
        "deposit_id": deposit_id,
        "amount": req.amount,
        "transfer_content": memo,
        "qr_url": qr_url,
        "expires_in": 300
    }

@app.post("/api/deposits/{deposit_id}/submitted")
async def api_deposit_submitted(deposit_id: int, x_telegram_init_data: str = Header(None)):
    user = verify_telegram_init_data(x_telegram_init_data or "")
    if not user or "id" not in user:
        raise HTTPException(status_code=401, detail="Xác thực Telegram thất bại.")
    uid = user["id"]
    full_name = user.get("first_name", "") + " " + user.get("last_name", "")
    username = user.get("username", "Không có")
    with db() as con:
        dep = con.execute("SELECT * FROM deposits WHERE id=? AND user_id=?", (deposit_id, uid)).fetchone()
        if not dep:
            raise HTTPException(status_code=404, detail="Không tìm thấy giao dịch nạp tiền.")
            
    msg_to_admin = (
        f"🔔 <b>CÓ YÊU CẦU NẠP TIỀN MỚI TỪ MINI APP</b>\n\n"
        f"👤 Khách hàng: {escape(full_name.strip())} (@{username})\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"💰 Số tiền: <b>{dep['amount']:,}đ</b>\n"
        f"📝 Nội dung CK: <code>{dep['note']}</code>\n"
        f"🧾 Mã đơn: #{deposit_id}\n\n"
        f"👉 Hãy kiểm tra ngân hàng và duyệt cộng tiền cho khách!"
    )
    for admin_id in ADMIN_IDS:
        try:
            url_api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            data_body = json.dumps({"chat_id": admin_id, "text": msg_to_admin, "parse_mode": "HTML"}).encode('utf-8')
            req = urllib.request.Request(url_api, data=data_body, headers={'Content-Type': 'application/json'}, method='POST')
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log.error(f"Lỗi gửi tin tới Admin: {e}")
    return {"status": "success", "message": "Đã gửi yêu cầu xác nhận tới Admin thành công!"}

async def main():
    init_db()
    
    def run_web():
        uvicorn.run(app, host="0.0.0.0", port=8080)
        
    threading.Thread(target=run_web, daemon=True).start()
    log.info("Web Server FastAPI đã khởi động trên cổng 8080...")

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_cmd))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    
    stop_event = asyncio.Event()
    await stop_event.wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot đã dừng.")

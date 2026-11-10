import os
import re
import asyncio
import logging
import json
import hmac
import hashlib
import time
import threading
from urllib.parse import parse_qsl
import urllib.request
import urllib.error
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from html import escape
from pathlib import Path
from urllib.parse import urlencode
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
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
QR_BANK = os.getenv('QR_BANK', 'VietinBank').strip()
QR_ACCOUNT = os.getenv('QR_ACCOUNT', '102886882160').strip()
QR_HOLDER = os.getenv('QR_HOLDER', 'BACH CONG HIEU HUY').strip()
QR_TEMPLATE = os.getenv('QR_TEMPLATE', 'compact').strip()
QR_SHOWINFO = os.getenv('QR_SHOWINFO', 'false').strip().lower()
QR_MEMO_PREFIX = 'SEVQR'
QR_MEMO_SUFFIX = os.getenv('QR_MEMO_SUFFIX', 'NAP').strip().upper() or 'NAP'
MINI_APP_URL = os.getenv('MINI_APP_URL', '').strip()
DB_POOL = None
logging.basicConfig(format='%(asctime)s | %(levelname)s | %(name)s | %(message)s', level=LOG_LEVEL)
log = logging.getLogger('key_shop_bot')

web_app = FastAPI(title='Jinny Cheat Mini App API')
telegram_application = None


USER_KB = ReplyKeyboardMarkup(
    ([[KeyboardButton('🛍 Mua Key', web_app=WebAppInfo(url=MINI_APP_URL))],
      [KeyboardButton('↩️ Trở về')]] if MINI_APP_URL.startswith('https://') else [[KeyboardButton('↩️ Trở về')]]),
    resize_keyboard=True, is_persistent=True)

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
    params = {
        'bank': QR_BANK, 'acc': QR_ACCOUNT, 'amount': str(amount), 'des': memo,
        'template': QR_TEMPLATE, 'showinfo': QR_SHOWINFO, 'holder': QR_HOLDER,
    }
    return 'https://vietqr.app/img?' + urlencode(params)
def is_admin(update): return bool(update.effective_user and update.effective_user.id in ADMIN_IDS)
def clean_lines(raw):
    out, seen = [], set()
    for line in raw.splitlines():
        v = re.sub(r'^\s*[•*\-]+\s*', '', line).strip().strip('` \t')
        if v and v not in seen:
            out.append(v); seen.add(v)
    return out

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
            amount BIGINT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', note TEXT, created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS balance_transactions (
            id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(user_id),
            admin_id BIGINT NOT NULL, action TEXT NOT NULL CHECK(action IN ('add','subtract','deposit')) ,
            amount BIGINT NOT NULL CHECK(amount > 0), balance_after BIGINT NOT NULL,
            note TEXT, created_at TEXT NOT NULL)""",
        r"""DO $$ DECLARE r RECORD; BEGIN
            FOR r IN
                SELECT conname FROM pg_constraint
                WHERE conrelid = 'public.product_options'::regclass
                  AND contype = 'c'
                  AND pg_get_constraintdef(oid) ILIKE '%duration%'
            LOOP
                EXECUTE format('ALTER TABLE public.product_options DROP CONSTRAINT %I', r.conname);
            END LOOP;
            ALTER TABLE public.product_options
                ADD CONSTRAINT product_options_duration_check
                CHECK (duration IN ('1h','1d','7d','30d'));
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$""",
        """INSERT INTO product_options(product_id,duration,duration_label,price,sell_price)
            SELECT p.id, d.duration, d.label, 0, 0
            FROM products p
            CROSS JOIN (VALUES ('7d','7 ngày'),('30d','30 ngày')) AS d(duration,label)
            ON CONFLICT(product_id,duration) DO NOTHING""",
        "CREATE INDEX IF NOT EXISTS idx_product_keys_stock ON product_keys(option_id,status)",
        "CREATE INDEX IF NOT EXISTS idx_purchases_user ON purchases(user_id,created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_users_username ON users(LOWER(username))",
        "CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen DESC)",
        "CREATE INDEX IF NOT EXISTS idx_deposits_user ON deposits(user_id,id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_deposits_status ON deposits(status,id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_balance_transactions_user ON balance_transactions(user_id,id DESC)",
    ]
    with db() as con:
        for index, sql in enumerate(statements, 1):
            try:
                con.execute(sql)
            except Exception:
                log.exception('Database migration statement %s failed', index)
                raise

def user_menu_text(user, balance):
    name = escape(user.full_name or 'Khách hàng')
    return (f'🎉 Xin chào, {name}!\n🛍 CHÀO MỪNG BẠN ĐẾN VỚI SHOP\n\n'
            f'━━━━━━━━━━━━━━━━━━\n👤 Tên: {name}\n🆔 ID: {user.id}\n'
            f'📛 Username: @{escape(user.username) if user.username else "Không có"}\n'
            f'💰 Số dư: {balance:,}đ\n━━━━━━━━━━━━━━━━━━\n\n'
            f'👇 Chọn chức năng bên dưới:')

def admin_menu_text(): return '🛠 QUẢN TRỊ SHOP\nChọn chức năng bên dưới:'

def maintenance_enabled():
    with db() as con:
        row=con.execute("SELECT setting_value FROM bot_settings WHERE setting_key='maintenance'").fetchone()
    return bool(row and row['setting_value']=='on')

def main_menu_markup():
    rows = []
    if MINI_APP_URL.startswith('https://'):
        rows.append([InlineKeyboardButton('🛍 Mở Mini App – Giao dịch nhanh', web_app=WebAppInfo(url=MINI_APP_URL))])
    rows.extend([
        [InlineKeyboardButton('👤 Tài Khoản', callback_data='menu:account'), InlineKeyboardButton('📜 Lịch Sử Mua', callback_data='menu:history')],
        [InlineKeyboardButton('🛍 Cửa Hàng', callback_data='shop:home'), InlineKeyboardButton('💳 Nạp Tiền', callback_data='menu:deposit')],
        [InlineKeyboardButton('📖 Hướng dẫn cách cài', callback_data='menu:guide')],
        [InlineKeyboardButton('🆘 Hỗ Trợ', callback_data='menu:support')],
    ])
    return InlineKeyboardMarkup(rows)

async def send_main_menu(message, user):
    with db() as con:
        row=con.execute('SELECT balance,is_locked FROM users WHERE user_id=?',(user.id,)).fetchone()
    if not row:
        await message.reply_text('Hãy gửi /start để bắt đầu.', reply_markup=USER_KB); return
    if row['is_locked']:
        await message.reply_text('⛔ Tài khoản của bạn đang bị khóa.', reply_markup=USER_KB); return
    if maintenance_enabled() and user.id not in ADMIN_IDS:
        await message.reply_text('🛠 Bot đang bảo trì. Vui lòng quay lại sau.', reply_markup=USER_KB); return
    await message.reply_text(user_menu_text(user,row['balance']),reply_markup=main_menu_markup(),parse_mode='HTML')

def inline_back(target='shop:home'): return InlineKeyboardMarkup([[InlineKeyboardButton('🌐 Quay lại', callback_data=target)]])

def get_categories():
    with db() as con: return con.execute('SELECT id,name,guide_url FROM categories ORDER BY id').fetchall()
def get_products(category_id):
    with db() as con: return con.execute('SELECT id,name FROM products WHERE category_id::text=?::text ORDER BY id', (category_id,)).fetchall()

def get_product_category_id(product_id):
    with db() as con:
        row=con.execute('SELECT category_id FROM products WHERE id=?',(product_id,)).fetchone()
    return row['category_id'] if row else 0

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
        await message.reply_text(user_menu_text(user, balance).replace(f'🆔 ID: {user.id}', f'🆔 ID: <code>{user.id}</code>'), reply_markup=main_menu_markup(), parse_mode='HTML')
    except Exception:
        log.exception('Failed to handle /start')
        await message.reply_text('⚠️ Lỗi cơ sở dữ liệu khi khởi động.')

async def admin_cmd(update, context):
    if not is_admin(update): await update.message.reply_text('⛔ Không có quyền admin.', reply_markup=USER_KB); return
    context.user_data.clear(); await update.message.reply_text(admin_menu_text(), reply_markup=ADMIN_KB)

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

async def show_quantity(q, option_id, context, user_id):
    with db() as con:
        o=con.execute('SELECT po.*,p.name AS product_name,p.category_id,c.name AS category_name FROM product_options po JOIN products p ON p.id::text=po.product_id::text JOIN categories c ON c.id::text=p.category_id::text WHERE po.id=?',(option_id,)).fetchone()
        u_row = con.execute('SELECT is_seller FROM users WHERE user_id=?',(user_id,)).fetchone()
    if not o: await q.edit_message_text('Gói sản phẩm không còn tồn tại.'); return
    is_seller = bool(u_row and u_row['is_seller'])
    current_price = o['sell_price'] if (is_seller and o['sell_price'] > 0) else o['price']
    context.user_data['selected_option']=option_id
    buttons=[
        [InlineKeyboardButton('🗝 Mua 1 Key',callback_data=f'shop:qty:{option_id}:1'),
         InlineKeyboardButton('🗝 Mua 2 Key',callback_data=f'shop:qty:{option_id}:2')],
        [InlineKeyboardButton('🗝 Mua 3 Key',callback_data=f'shop:qty:{option_id}:3'),
         InlineKeyboardButton('🗝 Mua 5 Key',callback_data=f'shop:qty:{option_id}:5')],
        [InlineKeyboardButton('🗝 Mua 10 Key',callback_data=f'shop:qty:{option_id}:10')],
        [InlineKeyboardButton('🔢 Mua số lượng khác',callback_data=f'shop:qty:{option_id}:other')],
        [InlineKeyboardButton('📂 Quay Lại',callback_data=f"shop:cat:{o['category_id']}")]
    ]
    is_sh = is_aim_hack_product(o['product_name'])
    if is_sh:
        src_text = 'ShopHenry' if o.get('shophenry_product_id') else 'Chưa gán ID'
    else:
        src_text = f"Kho: {stock_for_option(option_id)}"

    await q.edit_message_text(
        f"🛒 {escape(o['product_name'])}\n⏳ {escape(o['duration_label'])}"
        f"\n💵 Giá: {current_price:,}đ / key\n📦 Nguồn: {src_text}"
        f"\n\n🔑 Chọn số lượng Key muốn mua để tiến hành thanh toán:",
        reply_markup=InlineKeyboardMarkup(buttons),parse_mode='HTML')

async def show_confirm(q, option_id, qty, context, user_id):
    with db() as con:
        o=con.execute('SELECT po.*,p.name AS product_name,p.category_id,c.name AS category_name FROM product_options po JOIN products p ON p.id::text=po.product_id::text JOIN categories c ON c.id::text=p.category_id::text WHERE po.id=?',(option_id,)).fetchone()
        u_row = con.execute('SELECT is_seller FROM users WHERE user_id=?',(user_id,)).fetchone()
    if not o: await q.edit_message_text('Sản phẩm không còn tồn tại.'); return
    is_seller = bool(u_row and u_row['is_seller'])
    current_price = o['sell_price'] if (is_seller and o['sell_price'] > 0) else o['price']
    total=current_price*qty
    is_shophenry=is_aim_hack_product(o['product_name'])
    context.user_data.update(pending_purchase={'option_id':option_id,'quantity':qty,'total':total})
    payment_note = ('💳 Thanh toán: ShopHenry tự trừ số dư tài khoản nguồn. Bot không trừ số dư nội bộ.'
                    if is_shophenry else '💳 Thanh toán: Trừ số dư tài khoản trong bot.')
    await q.edit_message_text(f"🧾 XÁC NHẬN MUA HÀNG\n\n📁 Danh mục: {escape(o['category_name'])}\n📦 Sản phẩm: {escape(o['product_name'])}\n⏳ Thời hạn: {escape(o['duration_label'])}\n🔢 Số lượng: {qty}\n💰 Giá bán: {total:,}đ\n{payment_note}\n\nBạn xác nhận mua chứ?",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('✅ XÁC NHẬN MUA',callback_data='shop:confirm')],[InlineKeyboardButton('❌ HỦY',callback_data='shop:cancel')]]),parse_mode='HTML')

def shophenry_buy(product_id, quantity):
    if not SHOPHENRY_API_TOKEN:
        raise RuntimeError('Thiếu SHOPHENRY_API_TOKEN trong biến môi trường.')
    url = SHOPHENRY_API_URL + ('&' if '?' in SHOPHENRY_API_URL else '?') + 'action=buy'
    body = json.dumps({'product_id': int(product_id), 'quantity': int(quantity)}).encode('utf-8')
    req = urllib.request.Request(
        url, data=body, method='POST',
        headers={
            'Authorization': f'Bearer {SHOPHENRY_API_TOKEN}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode('utf-8', errors='replace')[:1000]
        try:
            payload = json.loads(raw)
        except Exception:
            raise RuntimeError(f'ShopHenry trả HTTP {exc.code}.')
        if not payload.get('success'):
            raise RuntimeError(str(payload.get('message') or f'ShopHenry từ chối đơn (HTTP {exc.code}).'))
    if not isinstance(payload, dict) or not payload.get('success'):
        message = payload.get('message', 'ShopHenry không xác nhận đơn hàng.') if isinstance(payload, dict) else 'Phản hồi ShopHenry không hợp lệ.'
        raise RuntimeError(str(message))
    data = payload.get('data') or {}
    raw_keys = data.get('keys') or data.get('key')
    if isinstance(raw_keys, list):
        keys = [str(x).strip() for x in raw_keys if str(x).strip()]
    elif isinstance(raw_keys, str):
        keys = [line.strip() for line in raw_keys.replace('\r', '').split('\n') if line.strip()]
        if len(keys) == 1 and quantity > 1 and ' | ' in keys[0]:
            keys = [x.strip() for x in keys[0].split(' | ') if x.strip()]
    else:
        keys = []
    order_code = str(data.get('order_code') or '')
    if len(keys) != int(quantity):
        raise RuntimeError(
            'ShopHenry đã báo thành công nhưng số key trả về không khớp số lượng. '
            + (f'Mã đơn: {order_code}. ' if order_code else '')
            + 'Bot chưa trừ tiền khách; admin cần kiểm tra đơn ShopHenry trước khi cho mua lại.'
        )
    return '\n'.join(keys), order_code

async def do_purchase(q, update, context):
    pending = context.user_data.pop('pending_purchase', None)
    if not pending:
        await q.edit_message_text('Phiên mua hàng đã hết hoặc đã được xử lý. Hãy mở cửa hàng lại nếu muốn mua đơn mới.')
        return

    uid = update.effective_user.id
    oid = pending['option_id']
    qty = int(pending['quantity'])
    con = db()
    supplier_order = ''
    key_text = ''
    opt = None
    total = 0
    is_shophenry = False
    try:
        con.execute('BEGIN')
        user = con.execute('SELECT balance,is_locked,is_seller FROM users WHERE user_id=? FOR UPDATE', (uid,)).fetchone()
        opt = con.execute('''SELECT po.*,p.name AS product_name,p.category_id,
            c.name AS category_name FROM product_options po
            JOIN products p ON p.id::text=po.product_id::text
            JOIN categories c ON c.id::text=p.category_id::text WHERE po.id=?''', (oid,)).fetchone()
        if not user or user['is_locked']:
            con.rollback()
            await q.edit_message_text('⛔ Tài khoản không thể mua hàng.')
            return
        if not opt:
            con.rollback()
            await q.edit_message_text('Sản phẩm không còn tồn tại.')
            return

        is_seller = bool(user['is_seller'])
        current_price = opt['sell_price'] if (is_seller and opt['sell_price'] > 0) else opt['price']
        total = current_price * qty
        is_shophenry = is_aim_hack_product(opt['product_name'])

        if is_shophenry:
            if not opt.get('shophenry_product_id'):
                con.rollback()
                await q.edit_message_text(
                    '⚠️ Sản phẩm Aim Hack chưa được gán product_id ShopHenry. Admin cần gán ID trước khi mua.'
                )
                return
            try:
                key_text, supplier_order = await asyncio.to_thread(
                    shophenry_buy, opt['shophenry_product_id'], qty
                )
                keys = []
            except Exception as api_error:
                con.rollback()
                log.warning('ShopHenry purchase failed for user=%s product=%s: %s',
                            uid, opt['shophenry_product_id'], api_error)
                await q.edit_message_text(
                    '❌ ShopHenry không xác nhận được đơn.\n'
                    + str(api_error)[:700]
                    + '\n\nBot không trừ số dư nội bộ. Nếu API đã tạo đơn, hãy gửi mã đơn cho admin kiểm tra và không bấm mua lại.'
                )
                return
        else:
            if user['balance'] < total:
                con.rollback()
                await q.edit_message_text(
                    f"❌ Số dư không đủ.\nCần: {total:,}đ\nHiện có: {user['balance']:,}đ"
                )
                return
            keys = con.execute(
                "SELECT id,key_value FROM product_keys WHERE option_id=? AND status='available' ORDER BY id LIMIT ? FOR UPDATE SKIP LOCKED",
                (oid, qty)
            ).fetchall()
            if len(keys) < qty:
                con.rollback()
                await q.edit_message_text(f"❌ Kho không đủ key. Hiện còn {len(keys)} key.")
                return
            key_text = '\n'.join(k['key_value'] for k in keys)

        stamp = now()
        if not is_shophenry:
            con.execute('UPDATE users SET balance=balance-? WHERE user_id=?', (total, uid))
            for k in keys:
                con.execute(
                    "UPDATE product_keys SET status='sold',sold_to=?,sold_at=? WHERE id=?",
                    (uid, stamp, k['id'])
                )

        con.execute(
            '''INSERT INTO purchases(
                user_id,category_name,product_name,duration_label,quantity,
                unit_price,total_price,keys_text,created_at,supplier_order_code
            ) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (uid, opt['category_name'], opt['product_name'], opt['duration_label'],
             qty, current_price, total, key_text, stamp, supplier_order or None)
        )
        con.commit()

    except Exception:
        con.rollback()
        log.exception('Purchase record failed (ShopHenry order=%s)', supplier_order or 'n/a')
        if is_shophenry and key_text:
            await q.edit_message_text(
                '⚠️ ShopHenry đã tạo đơn thành công nhưng bot không lưu được lịch sử. '
                'Không mua lại đơn này.\n'
                f'🧾 Mã đơn: {escape(supplier_order or "Chưa có mã đơn trong phản hồi API")}\n'
                'Admin cần kiểm tra lịch sử đơn.',
                parse_mode='HTML', reply_markup=inline_back('menu:home')
            )
            if q.message:
                await q.message.reply_text('🔑 KEY CỦA BẠN:', reply_markup=USER_KB)
                key_lines = key_text.splitlines()
                chunk = []
                chunk_len = 0
                for line in key_lines:
                    extra = len(line) + 1
                    if chunk and chunk_len + extra > 3400:
                        await q.message.reply_text(
                            '<pre>' + escape('\n'.join(chunk)) + '</pre>',
                            parse_mode='HTML', reply_markup=USER_KB
                        )
                        chunk = []
                        chunk_len = 0
                    chunk.append(line)
                    chunk_len += extra
                if chunk:
                    await q.message.reply_text(
                        '<pre>' + escape('\n'.join(chunk)) + '</pre>',
                        parse_mode='HTML', reply_markup=USER_KB
                    )
        else:
            if q.message:
                await q.message.reply_text(
                    '⚠️ Có lỗi khi ghi nhận đơn. Không bấm mua lại; hãy gửi mã đơn cho admin kiểm tra: '
                    + (supplier_order or 'chưa nhận được mã đơn')
                )
        return
    finally:
        con.close()

    success_msg = (
        f"🥳 MUA KEY {opt['product_name'].upper()} {opt['duration_label'].upper()} THÀNH CÔNG\n\n"
        f"Key :\n<pre>{escape(key_text)}</pre>\n\n"
        f"HÃY THOÁT RA MENU CHÍNH ĐỂ LẤY CÁCH CÀI NHÉ!"
    )
    buy_again_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton('🛒 Mua key tiếp', callback_data='shop:home')]
    ])
    
    await q.edit_message_text(success_msg, parse_mode='HTML', reply_markup=buy_again_kb)

async def user_history(q, uid):
    with db() as con: rows = con.execute('SELECT * FROM purchases WHERE user_id=? ORDER BY id DESC LIMIT 20', (uid,)).fetchall()
    if not rows:
        await q.edit_message_text('📜 Bạn chưa có lịch sử mua hàng.', reply_markup=inline_back('menu:home'))
        return
    out = ['📜 LỊCH SỬ MUA HÀNG']
    for r in rows: out.append(f"\n🧾 Đơn #{r['id']} · {r['created_at']}\n{r['product_name']} · {r['duration_label']}\nSố lượng: {r['quantity']} · {r['total_price']:,}đ")
    await q.edit_message_text('\n'.join(out), reply_markup=inline_back('menu:home'))

async def broadcast(bot, text, photo=None, status_message=None, reply_markup=None):
    with db() as con: 
        users = con.execute('SELECT user_id FROM users WHERE is_locked=FALSE').fetchall()
    
    total = len(users)
    if total == 0:
        if status_message:
            try:
                await status_message.edit_text('⚠️ Không có người dùng nào để gửi thông báo.', reply_markup=ADMIN_KB)
            except Exception:
                await bot.send_message(chat_id=status_message.chat_id, text='⚠️ Không có người dùng nào để gửi thông báo.', reply_markup=ADMIN_KB)
        return 0

    if status_message:
        try:
            await status_message.edit_text(f'🚀 Đang tiến hành gửi thông báo đến {total} người dùng...')
        except Exception:
            status_message = await bot.send_message(chat_id=status_message.chat_id, text=f'🚀 Đang tiến hành gửi thông báo đến {total} người dùng...')

    sent = 0
    for index, u in enumerate(users, 1):
        uid = u['user_id']
        try:
            if photo:
                await bot.send_photo(chat_id=uid, photo=photo, caption=text, parse_mode='HTML', reply_markup=reply_markup)
            else:
                await bot.send_message(chat_id=uid, text=text, parse_mode='HTML', reply_markup=reply_markup)
            sent += 1
            await asyncio.sleep(0.04)
        except Exception as e:
            log.info('Broadcast skipped user %s: %s', uid, e)

        if status_message and index % 30 == 0:
            try:
                await status_message.edit_text(f'🚀 Đang gửi thông báo... ({index}/{total} người dùng)')
            except Exception:
                pass

    if status_message:
        try:
            await status_message.edit_text(f'✅ Đã gửi thông báo thành công tới {sent}/{total} người dùng.', reply_markup=ADMIN_KB)
        except Exception:
            await bot.send_message(chat_id=status_message.chat_id, text=f'✅ Đã gửi thông báo thành công tới {sent}/{total} người dùng.', reply_markup=ADMIN_KB)
    return sent

async def show_news_emoji_page(target, context, page=0):
    stickers = context.bot_data.get('news_emoji_stickers') or []
    if not stickers:
        await target.edit_message_text(
            'Chưa tải bộ emoji. Hãy bấm ✨ Kho Emoji NewsEmoji trong /admin để tải lại.',
            reply_markup=inline_back('adm:menu')
        )
        return

    page_size = 15
    total = len(stickers)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(int(page), pages - 1))
    selected = stickers[page * page_size:(page + 1) * page_size]
    header = f'✨ BỘ EMOJI NEWS EMOJI\nTổng số: {total}\nTrang: {page + 1}/{pages}'
    lines = [header]
    entities = []
    offset = len((header + '\n').encode('utf-16-le')) // 2

    for index, sticker in enumerate(selected, start=page * page_size + 1):
        emoji = getattr(sticker, 'emoji', None) or '✨'
        prefix = f'{index:02d}. '
        lines.append(prefix + emoji)
        emoji_offset = offset + len(prefix.encode('utf-16-le')) // 2
        custom_id = getattr(sticker, 'custom_emoji_id', None)
        if custom_id:
            emoji_length = len(emoji.encode('utf-16-le')) // 2
            entities.append(MessageEntity(type='custom_emoji', offset=emoji_offset, length=emoji_length, custom_emoji_id=str(custom_id)))
        offset += len((prefix + emoji + '\n').encode('utf-16-le')) // 2

    rows = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton('⬅️ Trước', callback_data=f'adm:emoji:page:{page-1}'))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton('Tiếp ➡️', callback_data=f'adm:emoji:page:{page+1}'))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton('🔄 Tải lại bộ emoji', callback_data='adm:emoji'), InlineKeyboardButton('↩️ Admin', callback_data='adm:menu')])
    await target.edit_message_text('\n'.join(lines), entities=entities, reply_markup=InlineKeyboardMarkup(rows))

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ''
    user = update.effective_user
    if user and not is_admin(update):
        if maintenance_enabled():
            await q.edit_message_text('🛠 Bot đang bảo trì. Vui lòng quay lại sau.', reply_markup=inline_back('menu:home'))
            return
        with db() as con:
            status = con.execute('SELECT is_locked FROM users WHERE user_id=?', (user.id,)).fetchone()
        if status and status['is_locked']:
            await q.edit_message_text('⛔ Tài khoản của bạn đang bị khóa.')
            return
    if data.startswith('deposit:approve:') or data.startswith('deposit:reject:'):
        if not is_admin(update):
            await q.edit_message_text('⛔ Bạn không có quyền duyệt đơn.')
            return
        await handle_deposit_review(q, context, data)
        return
    if data.startswith('adm:') or data.startswith('admin:') or data.startswith('user:'):
        if not is_admin(update): await q.edit_message_text('⛔ Không có quyền truy cập.'); return
    
    if data == 'adm:broadcast:confirm':
        pending_bc = context.user_data.pop('pending_broadcast', None)
        
        if not pending_bc or not pending_bc.get('text'):
            msg_obj = q.message
            if msg_obj:
                raw_text = msg_obj.caption or msg_obj.text or ''
                if 'XEM TRƯỚC THÔNG BÁO:\n\n' in raw_text:
                    bc_text = raw_text.split('XEM TRƯỚC THÔNG BÁO:\n\n', 1)[-1]
                else:
                    bc_text = raw_text
                photo = msg_obj.photo[-1].file_id if msg_obj.photo else None
                pending_bc = {'text': bc_text, 'photo': photo}

        if not pending_bc or not pending_bc.get('text'):
            try:
                await q.edit_message_text('⚠️ Không tìm thấy nội dung thông báo. Vui lòng tạo lại thông báo từ đầu.', reply_markup=ADMIN_KB)
            except Exception:
                await q.message.reply_text('⚠️ Không tìm thấy nội dung thông báo. Vui lòng tạo lại thông báo từ đầu.', reply_markup=ADMIN_KB)
            return

        status_msg = q.message
        try:
            await q.message.delete()
            status_msg = await context.bot.send_message(chat_id=q.message.chat_id, text='🚀 Đang chuẩn bị gửi thông báo...')
        except Exception:
            pass

        await broadcast(context.bot, pending_bc['text'], pending_bc.get('photo'), status_message=status_msg)
        return

    elif data == 'adm:broadcast:cancel':
        context.user_data.pop('pending_broadcast', None)
        try:
            await q.edit_message_text('❌ Đã hủy gửi thông báo.', reply_markup=ADMIN_KB)
        except Exception:
            await q.message.reply_text('❌ Đã hủy gửi thông báo.', reply_markup=ADMIN_KB)
        return

    if data == 'menu:guide':
        cats = get_categories()
        if not cats:
            await q.edit_message_text('Hiện chưa có danh mục sản phẩm nào.', reply_markup=inline_back('menu:home'))
            return
        rows = [[InlineKeyboardButton(c['name'], callback_data=f"guide:cat:{c['id']}")] for c in cats]
        rows.append([InlineKeyboardButton('🌐 Quay lại', callback_data='menu:home')])
        await q.edit_message_text('📖 HƯỚNG DẪN CÁCH CÀI\n\nChọn sản phẩm bạn muốn xem cách cài đặt:', reply_markup=InlineKeyboardMarkup(rows))
    elif data.startswith('guide:cat:'):
        cid = int(data.split(':')[2])
        with db() as con:
            cat = con.execute('SELECT * FROM categories WHERE id=?', (cid,)).fetchone()
        if not cat:
            await q.edit_message_text('Danh mục không tồn tại.', reply_markup=inline_back('menu:home'))
            return
        c_name = escape(cat['name'])
        g_url = cat.get('guide_url')
        if not g_url:
            txt = f"CÁCH CÀI {c_name.upper()}\n\n⚠️ Hiện tại sản phẩm này chưa được cập nhật link hướng dẫn."
            kb = InlineKeyboardMarkup([[InlineKeyboardButton('🌐 Quay lại', callback_data='menu:guide')]])
        else:
            txt = f"CÁCH CÀI {c_name.upper()}"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton('🔗 Truy cập link hướng dẫn', url=g_url)],
                [InlineKeyboardButton('🌐 Quay lại', callback_data='menu:guide')]
            ])
        await q.edit_message_text(txt, reply_markup=kb, parse_mode='HTML')
    elif data == 'adm:addguide':
        cats = get_categories()
        if not cats:
            await q.edit_message_text('Chưa có danh mục sản phẩm ngoài nào.', reply_markup=ADMIN_KB)
            return
        rows = [[InlineKeyboardButton(c['name'], callback_data=f"adm:guidecat:{c['id']}")] for c in cats]
        rows.append([InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')])
        await q.edit_message_text('Chọn sản phẩm ngoài cần thêm/sửa link cách cài:', reply_markup=InlineKeyboardMarkup(rows))
    elif data.startswith('adm:guidecat:'):
        cid = int(data.split(':')[2])
        context.user_data.update(flow='save_guide_url', guide_category_id=cid)
        if q.message:
            await q.message.reply_text('Gửi đường link (URL) hướng dẫn cài đặt cho sản phẩm này:')
    elif data == 'adm:stats':
        with db() as con:
            user_count = con.execute('SELECT COUNT(*) AS n FROM users').fetchone()['n']
            total_deposit = con.execute("SELECT SUM(amount) AS total FROM deposits WHERE status='approved'").fetchone()['total'] or 0
            total_balance = con.execute("SELECT SUM(balance) AS total FROM users").fetchone()['total'] or 0
        txt = (
            f"📊 THỐNG KÊ HỆ THỐNG\n\n"
            f"👥 Tổng số người dùng: {user_count:,}\n"
            f"💰 Tổng tiền đã nạp thành công: {total_deposit:,}đ\n"
            f"💼 Tổng số dư ví hiện tại của user: {total_balance:,}đ"
        )
        await q.edit_message_text(txt, reply_markup=inline_back('adm:menu'), parse_mode='HTML')
    elif data.startswith('deposit:submitted:'):
        deposit_id = int(data.split(':')[2])
        with db() as con:
            dep = con.execute('SELECT d.*,u.full_name,u.username FROM deposits d JOIN users u ON u.user_id=d.user_id WHERE d.id=? AND d.user_id=? FOR UPDATE', (deposit_id, user.id)).fetchone()
            if not dep:
                await q.edit_message_caption(caption='Không tìm thấy đơn nạp.', reply_markup=None)
                return
            if dep['status'] != 'pending':
                await q.edit_message_caption(caption='Đơn này đã được gửi hoặc xử lý.', reply_markup=None)
                return
            con.execute("UPDATE deposits SET status='submitted' WHERE id=?", (deposit_id,))
        await q.edit_message_caption(caption=f"📨 Đã gửi yêu cầu xác nhận nạp #{deposit_id}. Admin sẽ kiểm tra giao dịch.\n💰 Số tiền: {dep['amount']:,}đ\n📝 Nội dung: {dep['note']}", reply_markup=None)
        await notify_admins(context, deposit_id, dep['amount'], dep['note'], dep['full_name'], user.id, dep['username'])
    elif data.startswith('deposit:cancel:'):
        deposit_id = int(data.split(':')[2])
        with db() as con:
            con.execute("UPDATE deposits SET status='cancelled' WHERE id=? AND user_id=? AND status='pending'", (deposit_id, user.id))
        await q.edit_message_caption(caption='❌ Đơn nạp tiền đã được hủy.', reply_markup=None)
    elif data == 'menu:home':
        context.user_data.pop('flow', None)
        with db() as con:
            row = con.execute('SELECT balance,is_locked FROM users WHERE user_id=?', (user.id,)).fetchone()
        if not row:
            await q.edit_message_text('Bạn chưa có tài khoản. Hãy gửi /start để bắt đầu.')
        elif row['is_locked']:
            await q.edit_message_text('⛔ Tài khoản của bạn đang bị khóa.')
        else:
            await q.edit_message_text(user_menu_text(user, row['balance']), reply_markup=main_menu_markup(), parse_mode='HTML')
    elif data == 'menu:account':
        with db() as con:
            row = con.execute('SELECT balance,is_locked FROM users WHERE user_id=?', (user.id,)).fetchone()
        if not row:
            await q.edit_message_text('Hãy gửi /start trước.')
        else:
            txt = (f"👤 TÀI KHOẢN\nTên: {escape(user.full_name)}\nID: {user.id}\n"
                   f"Số dư: {row['balance']:,}đ\nTrạng thái: {'🔒 Đã khóa' if row['is_locked'] else '✅ Hoạt động'}")
            await q.edit_message_text(txt, reply_markup=inline_back('menu:home'), parse_mode='HTML')
    elif data == 'menu:history':
        await user_history(q, user.id)
    elif data == 'menu:deposit':
        context.user_data['flow'] = 'deposit_amount'
        await q.edit_message_text('💳 NẠP TIỀN\n\nNhập số tiền muốn nạp (VNĐ):', reply_markup=inline_back('menu:home'))
    elif data == 'menu:support':
        await q.edit_message_text('🆘 HỖ TRỢ\nVui lòng liên hệ quản trị viên.', reply_markup=inline_back('menu:home'))
    elif data == 'shop:home': await show_shop(q.message, edit=True)
    elif data.startswith('shop:cat:'): await show_category(q, int(data.split(':')[2]), user.id)
    elif data.startswith('shop:option:'): await show_quantity(q, int(data.split(':')[2]), context, user.id)
    elif data.startswith('shop:qty:'):
        _, _, oid, qty = data.split(':')
        if qty == 'other':
            context.user_data['flow'] = 'custom_quantity'
            context.user_data['selected_option'] = int(oid)
            if q.message:
                await q.message.reply_text('Nhập số lượng key muốn mua (1–1.000):', reply_markup=USER_KB)
        else:
            await show_confirm(q, int(oid), int(qty), context, user.id)
    elif data == 'shop:confirm': await do_purchase(q, update, context)
    elif data == 'shop:cancel':
        context.user_data.pop('pending_purchase', None)
        await q.edit_message_text('Đã hủy đơn mua hàng.', reply_markup=inline_back('shop:home'))
    elif data == 'adm:menu': await q.edit_message_text(admin_menu_text(), reply_markup=ADMIN_KB)
    elif data == 'adm:emoji':
        try:
            sticker_set = await context.bot.get_sticker_set(name='NewsEmoji')
            sticker_type = getattr(sticker_set, 'sticker_type', None)
            sticker_type_value = getattr(sticker_type, 'value', sticker_type)
            if sticker_type_value and str(sticker_type_value).lower() != 'custom_emoji':
                await q.edit_message_text(
                    f'⚠️ Bộ NewsEmoji trả về loại {sticker_type}, không phải bộ custom emoji.',
                    reply_markup=inline_back('adm:menu')
                )
                return
            stickers = list(getattr(sticker_set, 'stickers', []) or [])
            if not stickers:
                await q.edit_message_text('⚠️ Bộ NewsEmoji hiện không có emoji nào hoặc bot không đọc được bộ này.', reply_markup=inline_back('adm:menu'))
                return
            context.bot_data['news_emoji_stickers'] = stickers
            await q.edit_message_text(
                f'✅ Đã tải được {len(stickers)} emoji từ bộ NewsEmoji. Đang hiển thị trang đầu...',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('✨ Xem toàn bộ emoji', callback_data='adm:emoji:page:0')], [InlineKeyboardButton('↩️ Admin', callback_data='adm:menu')]])
            )
        except Exception as exc:
            log.exception('Cannot load Telegram emoji set NewsEmoji')
            await q.edit_message_text(
                '⚠️ Không tải được bộ NewsEmoji. Telegram có thể không cho bot truy cập bộ này hoặc tên bộ không hợp lệ.\n'
                f'Chi tiết: {escape(str(exc)[:250])}',
                reply_markup=inline_back('adm:menu'), parse_mode='HTML'
            )
    elif data.startswith('adm:emoji:page:'):
        try:
            page = int(data.rsplit(':', 1)[1])
            await show_news_emoji_page(q, context, page)
        except Exception:
            log.exception('Cannot display NewsEmoji page')
            await q.edit_message_text('⚠️ Không hiển thị được trang emoji này.', reply_markup=inline_back('adm:menu'))
    elif data == 'adm:maintenance':
        with db() as con:
            current = con.execute("SELECT setting_value FROM bot_settings WHERE setting_key='maintenance'").fetchone()
            new_value = 'off' if current and current['setting_value'] == 'on' else 'on'
            con.execute("INSERT INTO bot_settings(setting_key,setting_value,updated_at) VALUES('maintenance',?,?) ON CONFLICT(setting_key) DO UPDATE SET setting_value=EXCLUDED.setting_value,updated_at=EXCLUDED.updated_at", (new_value, now()))
        label = 'ĐANG BẬT' if new_value == 'on' else 'ĐANG TẮT'
        await q.edit_message_text(f'🛠 CHẾ ĐỘ BẢO TRÌ: {label}\n' + ('Người dùng thường không thể sử dụng bot.' if new_value == 'on' else 'Người dùng có thể sử dụng bot bình thường.'), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔄 Bật / Tắt bảo trì', callback_data='adm:maintenance')], [InlineKeyboardButton('↩️ Quay lại Admin', callback_data='adm:menu')]]))
    elif data == 'adm:broadcast':
        context.user_data['flow'] = 'broadcast_content'
        if q.message:
            await q.message.reply_text('Gửi nội dung thông báo (có thể kèm ảnh hoặc chỉ văn bản) muốn gửi đến tất cả người dùng:')
    elif data == 'adm:usercheck':
        context.user_data['flow'] = 'user_check_id'
        if q.message:
            await q.message.reply_text('Nhập Telegram ID hoặc @username cần kiểm tra:')
    elif data == 'adm:addproduct':
        await q.edit_message_text('Chọn loại sản phẩm muốn thêm:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('📁 Sản phẩm ngoài (danh mục)', callback_data='adm:addcat')], [InlineKeyboardButton('📦 Sản phẩm trong (sản phẩm)', callback_data='adm:addinner')], [InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data == 'adm:addcat':
        context.user_data['flow'] = 'add_category_name'
        if q.message:
            await q.message.reply_text('Nhập tên danh mục ngoài:')
    elif data == 'adm:mapshop':
        cats = get_categories()
        if not cats:
            if q.message:
                await q.message.reply_text('Chưa có danh mục sản phẩm.')
            return
        await q.edit_message_text('Chọn danh mục chứa sản phẩm cần gán ID ShopHenry:', reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(c['name'], callback_data='adm:mapshopcat:{}'.format(c['id']))] for c in cats] + [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data.startswith('adm:mapshopcat:'):
        cid = int(data.split(':')[2])
        ps = get_products(cid)
        await q.edit_message_text('Chọn sản phẩm cần gán ID ShopHenry:', reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(p['name'], callback_data='adm:mapshopprod:{}'.format(p['id']))] for p in ps] + [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:mapshop')]]))
    elif data.startswith('adm:mapshopprod:'):
        pid = int(data.split(':')[2])
        with db() as con: options = con.execute('SELECT id,duration_label,shophenry_product_id FROM product_options WHERE product_id=? ORDER BY id', (pid,)).fetchall()
        await q.edit_message_text('Chọn gói thời hạn cần gán ID ShopHenry:', reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"{o['duration_label']} · ID: {o['shophenry_product_id'] or 'Chưa gán'}", callback_data=f"adm:mapshopopt:{o['id']}")] for o in options] + [[InlineKeyboardButton('↩️ Quay lại', callback_data=f'adm:mapshopcat:{get_product_category_id(pid)}')]]))
    elif data.startswith('adm:mapshopopt:'):
        oid = int(data.split(':')[2])
        context.user_data.update(flow='map_shophenry_id', shophenry_target_option=oid)
        if q.message:
            await q.message.reply_text('Nhập product_id của đúng gói thời hạn này trên ShopHenry (chỉ nhập ID dạng số):')
    elif data == 'adm:addinner':
        cats = get_categories()
        if not cats:
            if q.message:
                await q.message.reply_text('Chưa có danh mục ngoài. Hãy thêm danh mục trước.')
            return
        await q.edit_message_text('Chọn danh mục ngoài chứa sản phẩm mới:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(c['name'], callback_data=f"adm:innercat:{c['id']}")] for c in cats] + [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data.startswith('adm:innercat:'):
        try:
            cid = int(data.split(':')[2])
            with db() as con:
                category = con.execute('SELECT id, name FROM categories WHERE id=?', (cid,)).fetchone()
            if not category:
                context.user_data.pop('flow', None)
                context.user_data.pop('new_product_category', None)
                await q.edit_message_text('Danh mục không còn tồn tại. Hãy mở /admin và chọn lại.', reply_markup=ADMIN_KB)
                return
            context.user_data['new_product_category'] = cid
            context.user_data['flow'] = 'add_product_name'
            await q.edit_message_text(
                f"📁 Danh mục: {escape(category['name'])}\n\nNhập tên sản phẩm bên trong:",
                reply_markup=inline_back('adm:addinner'), parse_mode='HTML'
            )
        except Exception:
            log.exception('Admin inner-category callback failed')
            if q.message:
                await q.message.reply_text('⚠️ Không thể chọn danh mục. Vui lòng thử lại.')
        return
    elif data == 'adm:renamecat':
        cats = get_categories()
        await q.edit_message_text('Chọn danh mục ngoài muốn đổi tên:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(c['name'], callback_data=f"adm:renamecatid:{c['id']}")] for c in cats] + [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data.startswith('adm:renamecatid:'):
        context.user_data.update(flow='rename_category', rename_category_id=int(data.split(':')[2]))
        if q.message:
            await q.message.reply_text('Nhập tên mới cho danh mục ngoài:')
    elif data == 'adm:detailproduct':
        cats = get_categories()
        await q.edit_message_text('📋 CHI TIẾT SẢN PHẨM\nChọn danh mục:', reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(c['name'], callback_data=f"adm:detailcat:{c['id']}")] for c in cats] +
            [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data.startswith('adm:detailcat:'):
        cid = int(data.split(':')[2]); ps = get_products(cid)
        await q.edit_message_text('Chọn sản phẩm cần xem/sửa:', reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(p['name'], callback_data=f"adm:detailprod:{p['id']}")] for p in ps] +
            [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:detailproduct')]]))
    elif data.startswith('adm:detailprod:'):
        pid = int(data.split(':')[2])
        with db() as con:
            opts = con.execute('SELECT * FROM product_options WHERE product_id::text=?::text ORDER BY id', (pid,)).fetchall()
        rows = [[InlineKeyboardButton(f"{o['duration_label']} · Giá: {o['price']:,}đ | Sell: {o['sell_price']:,}đ", callback_data=f"adm:detailoption:{o['id']}")] for o in opts]
        rows.append([InlineKeyboardButton('↩️ Quay lại', callback_data='adm:detailproduct')])
        await q.edit_message_text('📋 CHI TIẾT SẢN PHẨM\nChọn gói muốn thay đổi giá:', reply_markup=InlineKeyboardMarkup(rows))
    elif data.startswith('adm:detailoption:'):
        oid = int(data.split(':')[2])
        context.user_data.update(flow='edit_option_price', edit_option_id=oid)
        if q.message:
            await q.message.reply_text('Nhập giá thường mới bằng số VNĐ (ví dụ 50000):')
    elif data == 'adm:editproduct':
        cats = get_categories()
        await q.edit_message_text('Chọn danh mục chứa sản phẩm cần sửa giá thường:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(c['name'], callback_data=f"adm:editcat:{c['id']}")] for c in cats] + [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data.startswith('adm:editcat:'):
        cid = int(data.split(':')[2]); ps = get_products(cid); await q.edit_message_text('Chọn sản phẩm cần sửa:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(p['name'], callback_data=f"adm:editprod:{p['id']}")] for p in ps] + [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data.startswith('adm:editprod:'):
        pid = int(data.split(':')[2])
        with db() as con: opts = con.execute('SELECT * FROM product_options WHERE product_id::text=?::text ORDER BY duration', (pid,)).fetchall()
        await q.edit_message_text('Chọn gói thời hạn cần sửa giá thường:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{o['duration_label']} · {o['price']:,}đ", callback_data=f"adm:editoption:{o['id']}")] for o in opts] + [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data.startswith('adm:editoption:'):
        oid = int(data.split(':')[2]); context.user_data.update(flow='edit_option_price', edit_option_id=oid)
        if q.message:
            await q.message.reply_text('Nhập giá thường mới bằng số VNĐ (ví dụ 50000):')
    elif data == 'adm:editsellprice':
        cats = get_categories()
        await q.edit_message_text('Chọn danh mục chứa sản phẩm cần chỉnh giá Sell:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(c['name'], callback_data=f"adm:editsellcat:{c['id']}")] for c in cats] + [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data.startswith('adm:editsellcat:'):
        cid = int(data.split(':')[2]); ps = get_products(cid); await q.edit_message_text('Chọn sản phẩm cần sửa giá Sell:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(p['name'], callback_data=f"adm:editsellprod:{p['id']}")] for p in ps] + [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data.startswith('adm:editsellprod:'):
        pid = int(data.split(':')[2])
        with db() as con: opts = con.execute('SELECT * FROM product_options WHERE product_id::text=?::text ORDER BY duration', (pid,)).fetchall()
        await q.edit_message_text('Chọn gói thời hạn cần sửa giá Sell:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{o['duration_label']} · Giá Sell: {o['sell_price']:,}đ", callback_data=f"adm:editselloption:{o['id']}")] for o in opts] + [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data.startswith('adm:editselloption:'):
        oid = int(data.split(':')[2]); context.user_data.update(flow='edit_sell_price_val', edit_option_id=oid)
        if q.message:
            await q.message.reply_text('Nhập mức giá Sell mới bằng số VNĐ:')
    elif data == 'adm:delproduct':
        await q.edit_message_text('Chọn phần cần xóa:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('📁 Xóa danh mục ngoài', callback_data='adm:delcat')], [InlineKeyboardButton('📦 Xóa sản phẩm trong', callback_data='adm:delinner')], [InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data == 'adm:delcat':
        cats = get_categories()
        await q.edit_message_text('Chọn danh mục ngoài cần xóa:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(c['name'], callback_data=f"adm:delcatconfirm:{c['id']}")] for c in cats] + [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data.startswith('adm:delcatconfirm:'):
        cid = int(data.split(':')[2]); context.user_data['delete_target'] = ('category', cid); await q.edit_message_text('Xác nhận xóa danh mục và toàn bộ sản phẩm/key bên trong?', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⚠️ XÁC NHẬN XÓA', callback_data='adm:deleteconfirm')], [InlineKeyboardButton('Hủy', callback_data='adm:menu')]]))
    elif data == 'adm:delinner':
        cats = get_categories()
        await q.edit_message_text('Chọn danh mục chứa sản phẩm cần xóa:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(c['name'], callback_data=f"adm:delinnercat:{c['id']}")] for c in cats] + [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data.startswith('adm:delinnercat:'):
        cid = int(data.split(':')[2]); ps = get_products(cid); await q.edit_message_text('Chọn sản phẩm cần xóa:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(p['name'], callback_data=f"adm:delinnerconfirm:{p['id']}")] for p in ps] + [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data.startswith('adm:delinnerconfirm:'):
        pid = int(data.split(':')[2]); context.user_data['delete_target'] = ('product', pid); await q.edit_message_text('Xác nhận xóa sản phẩm và các gói/key bên trong?', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⚠️ XÁC NHẬN XÓA', callback_data='adm:deleteconfirm')], [InlineKeyboardButton('Hủy', callback_data='adm:menu')]]))
    elif data == 'adm:deleteconfirm':
        target = context.user_data.pop('delete_target', None)
        if target:
            with db() as con: con.execute('DELETE FROM categories WHERE id=?', (target[1],)) if target[0] == 'category' else con.execute('DELETE FROM products WHERE id=?', (target[1],))
        await q.edit_message_text('✅ Đã xóa thành công.', reply_markup=ADMIN_KB)
    elif data == 'adm:addkeys':
        cats = get_categories()
        await q.edit_message_text('Chọn danh mục:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(c['name'], callback_data=f"adm:keycat:{c['id']}")] for c in cats] + [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data.startswith('adm:keycat:'):
        cid = int(data.split(':')[2]); ps = get_products(cid)
        await q.edit_message_text('Chọn sản phẩm bên trong để thêm key:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(p['name'], callback_data=f"adm:keyproduct:{p['id']}")] for p in ps] + [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data.startswith('adm:keyproduct:'):
        pid = int(data.split(':')[2])
        context.user_data['key_product_id'] = pid
        with db() as con:
            options = con.execute('SELECT duration,duration_label FROM product_options WHERE product_id::text=?::text ORDER BY id', (pid,)).fetchall()
        rows = [[InlineKeyboardButton(f"🔑 KEY {o['duration_label']}", callback_data=f"adm:keyduration:{o['duration']}")] for o in options]
        rows.append([InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')])
        await q.edit_message_text('Chọn gói thời hạn:', reply_markup=InlineKeyboardMarkup(rows))
    elif data.startswith('adm:keyduration:'):
        dur = data.split(':')[2]
        pid = context.user_data.get('key_product_id')
        if not pid:
            if q.message:
                await q.message.reply_text('⚠️ Phiên làm việc đã hết hạn. Vui lòng bấm /admin để thực hiện lại.', reply_markup=ADMIN_KB)
            return
        with db() as con:
            opt = con.execute('SELECT id,duration_label FROM product_options WHERE product_id::text=?::text AND duration=?', (pid, dur)).fetchone()
            if not opt:
                label = DURATIONS[dur]
                cur_ins = con.execute('INSERT INTO product_options(product_id, duration, duration_label, price, sell_price) VALUES (?, ?, ?, 0, 0) RETURNING id', (pid, dur, label))
                opt_id = cur_ins.fetchone()['id']
                con.commit()
            else:
                opt_id = opt['id']
        context.user_data.update(flow='add_product_keys', key_option_id=opt_id, key_duration=dur)
        label = opt['duration_label'] if opt else DURATIONS[dur]
        if q.message:
            await q.message.reply_text(f'Gửi danh sách key {label}, mỗi key một dòng:')
    elif data == 'adm:stock':
        cats = get_categories()
        await q.edit_message_text('Chọn danh mục để kiểm tra kho:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(c['name'], callback_data=f"adm:stockcat:{c['id']}")] for c in cats] + [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data.startswith('adm:stockcat:'):
        cid = int(data.split(':')[2]); ps = get_products(cid); await q.edit_message_text('Chọn sản phẩm:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(p['name'], callback_data=f"adm:stockprod:{p['id']}")] for p in ps] + [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data.startswith('adm:stockprod:'):
        pid = int(data.split(':')[2])
        with db() as con: rows = con.execute("SELECT po.*,COUNT(pk.id) FILTER (WHERE pk.status='available') AS available,COUNT(pk.id) AS total FROM product_options po LEFT JOIN product_keys pk ON pk.option_id=po.id WHERE po.product_id::text=?::text GROUP BY po.id ORDER BY po.duration", (pid,)).fetchall()
        txt = '📦 KHO SẢN PHẨM\n' + '\n'.join(f"{r['duration_label']}: còn {r['available']} / tổng {r['total']} · Thường: {r['price']:,}đ | Sell: {r['sell_price']:,}đ" for r in rows) if rows else 'Sản phẩm chưa có gói thời hạn.'
        await q.edit_message_text(txt, reply_markup=inline_back('adm:menu'))
    elif data.startswith('user:'):
        await admin_user_callback(q, context, data)

async def admin_user_callback(q, context, data):
    parts = data.split(':'); uid = int(parts[2])
    if parts[1] == 'deposit':
        with db() as con: rows = con.execute('SELECT * FROM deposits WHERE user_id=? ORDER BY id DESC LIMIT 20', (uid,)).fetchall()
        body = '📥 LỊCH SỬ NẠP TIỀN\n' + '\n'.join(f"#{r['id']} · {r['amount']:,}đ · {r['status']} · {r['note'] or ''} · {r['created_at']}" for r in rows) if rows else 'Chưa có lịch sử nạp tiền.'
        await q.message.reply_text(body)
    elif parts[1] == 'purchase':
        with db() as con: rows = con.execute('SELECT * FROM purchases WHERE user_id=? ORDER BY id DESC LIMIT 20', (uid,)).fetchall()
        body = '🧾 LỊCH SỬ MUA KEY\n' + '\n'.join(f"#{r['id']} {r['product_name']} · {r['duration_label']} · {r['quantity']} key · {r['total_price']:,}đ" for r in rows) if rows else 'Chưa có lịch sử mua key.'
        await q.message.reply_text(body)
    elif parts[1] in ('lock', 'unlock'):
        locked = parts[1] == 'lock'
        with db() as con:
            con.execute('UPDATE users SET is_locked=? WHERE user_id=?', (locked, uid))
        await q.message.reply_text('🔒 Đã khóa tài khoản.' if locked else '🔓 Đã mở khóa tài khoản.')
        await show_admin_user(q.message, uid)
    elif parts[1] in ('setsell', 'removesell'):
        is_sel = parts[1] == 'setsell'
        with db() as con:
            con.execute('UPDATE users SET is_seller=? WHERE user_id=?', (is_sel, uid))
        await q.message.reply_text('👑 Đã cấp quyền Sell cho user.' if is_sel else '❌ Đã xóa quyền Sell của user.')
        await show_admin_user(q.message, uid)
    elif parts[1] in ('add', 'subtract'):
        context.user_data.update(flow='admin_balance_amount', balance_action=parts[1], target_user_id=uid)
        verb = 'cộng vào' if parts[1] == 'add' else 'trừ khỏi'
        await q.message.reply_text(f'Nhập số tiền muốn {verb} tài khoản ID {uid} (VNĐ):')

async def show_admin_user(msg, uid):
    with db() as con: r = con.execute('SELECT * FROM users WHERE user_id=?', (uid,)).fetchone()
    if not r:
        await msg.reply_text('Không tìm thấy người dùng.', reply_markup=ADMIN_KB); return
    uname = '@' + r['username'] if r['username'] else 'Không có'
    joined = r['joined_at'] or 'Không rõ'; seen = r['last_seen'] or 'Không rõ'
    is_sel = bool(r['is_seller'])
    profile = f"\n🔗 <a href=\"https://t.me/{escape(r['username'])}\">Mở hồ sơ Telegram</a>" if r['username'] else ''
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton('➕ Cộng tiền', callback_data=f'user:add:{uid}'), InlineKeyboardButton('➖ Trừ tiền', callback_data=f'user:subtract:{uid}')],
        [InlineKeyboardButton('👑 Cấp Sell', callback_data=f'user:setsell:{uid}'), InlineKeyboardButton('❌ Xóa Sell', callback_data=f'user:removesell:{uid}')],
        [InlineKeyboardButton('📥 Lịch sử nạp', callback_data=f'user:deposit:{uid}'), InlineKeyboardButton('🧾 Lịch sử mua', callback_data=f'user:purchase:{uid}')],
        [InlineKeyboardButton('🔒 Khóa tài khoản', callback_data=f'user:lock:{uid}'), InlineKeyboardButton('🔓 Mở khóa', callback_data=f'user:unlock:{uid}')],
        [InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')],
    ])
    seller_status_text = '✅ Đã cấp (Có giá riêng)' if is_sel else '❌ Không có'
    await msg.reply_text(
        f"👤 THÔNG TIN NGƯỜI DÙNG\n\n🆔 ID: {uid}\n📛 Username: {escape(uname)}\n👤 Tên: {escape(r['full_name'] or 'Không có')}\n💰 Số dư: {r['balance']:,}đ\n👑 Cấp độ Sell: {seller_status_text}\n🔐 Trạng thái: {'🔒 Đã khóa' if r['is_locked'] else '✅ Hoạt động'}\n📅 Tham gia: {escape(joined)}\n🕒 Hoạt động gần nhất: {escape(seen)}{profile}",
        reply_markup=kb, parse_mode='HTML'
    )

async def notify_admins(context, deposit_id, amount, memo, full_name, uid, username):
    username_text = '@' + username if username else 'Không có'
    body = (f"💳 YÊU CẦU XÁC NHẬN NẠP TIỀN\n\n🧾 Mã đơn: #{deposit_id}\n💰 Số tiền: {amount:,}đ\n📝 Nội dung CK: {memo}\n👤 Tên: {escape(full_name or 'Không có')}\n🆔 ID: {uid}\n📛 Username: {escape(username_text)}\n🕒 Thời gian xác nhận: {now_vn()} (GMT+7)\n\nKiểm tra giao dịch ngân hàng trước khi duyệt.")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton('✅ DUYỆT', callback_data=f'deposit:approve:{deposit_id}'), InlineKeyboardButton('❌ TỪ CHỐI', callback_data=f'deposit:reject:{deposit_id}')]])
    for admin_id in ADMIN_IDS:
        try: await context.bot.send_message(chat_id=admin_id, text=body, reply_markup=kb, parse_mode='HTML')
        except Exception: log.exception('Cannot notify admin %s about deposit %s', admin_id, deposit_id)

async def handle_deposit_review(q, context, data):
    _, action, raw_id = data.split(':', 2)
    deposit_id = int(raw_id)
    try:
        with db() as con:
            dep = con.execute('SELECT * FROM deposits WHERE id=? FOR UPDATE', (deposit_id,)).fetchone()
            if not dep:
                await q.edit_message_text('Không tìm thấy đơn nạp tiền.'); return
            if dep['status'] not in ('pending', 'submitted'):
                await q.edit_message_text(f"Đơn #{deposit_id} đã được xử lý trước đó: {dep['status']}."); return
            if action == 'approve':
                con.execute('UPDATE users SET balance=balance+? WHERE user_id=?', (dep['amount'], dep['user_id']))
                row = con.execute('SELECT balance FROM users WHERE user_id=?', (dep['user_id'],)).fetchone()
                con.execute("INSERT INTO balance_transactions(user_id,admin_id,action,amount,balance_after,note,created_at) VALUES(?,?,'deposit',?,?,?,?)", (dep['user_id'], q.from_user.id, dep['amount'], row['balance'], f"Duyệt nạp #{deposit_id}", now()))
                con.execute("UPDATE deposits SET status='approved' WHERE id=?", (deposit_id,))
                result = f"✅ Đã duyệt đơn nạp #{deposit_id}. Đã cộng {dep['amount']:,}đ. Số dư mới: {row['balance']:,}đ."
                user_notice = f"✅ Yêu cầu nạp tiền #{deposit_id} đã được admin duyệt. Số tiền {dep['amount']:,}đ đã cộng vào tài khoản."
            else:
                con.execute("UPDATE deposits SET status='rejected' WHERE id=?", (deposit_id,))
                result = f"❌ Đã từ chối đơn nạp #{deposit_id}."
                user_notice = f"❌ Yêu cầu nạp tiền #{deposit_id} đã bị từ chối. Nếu bạn đã chuyển tiền, vui lòng liên hệ admin."
        await q.edit_message_text(result)
        try: await context.bot.send_message(chat_id=dep['user_id'], text=user_notice)
        except Exception: log.exception('Cannot notify user about deposit %s', deposit_id)
    except Exception:
        log.exception('Deposit review failed for %s', deposit_id)
        await q.message.reply_text('Có lỗi khi xử lý đơn nạp. Kiểm tra Logs trước khi thao tác lại.')

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message; text = (msg.text or '').strip(); flow = context.user_data.get('flow'); user = update.effective_user
    if text == '↩️ Trở về':
        context.user_data.clear()
        await send_main_menu(msg, user)
        return
    if user and not is_admin(update):
        if maintenance_enabled():
            await msg.reply_text('🛠 Bot đang bảo trì. Vui lòng quay lại sau.', reply_markup=USER_KB); return
        with db() as con:
            locked = con.execute('SELECT is_locked FROM users WHERE user_id=?', (user.id,)).fetchone()
        if locked and locked['is_locked']:
            await msg.reply_text('⛔ Tài khoản của bạn đang bị khóa.', reply_markup=USER_KB); return
    
    if flow == 'save_guide_url' and is_admin(update):
        cid = context.user_data.get('guide_category_id')
        try:
            with db() as con:
                con.execute('UPDATE categories SET guide_url=? WHERE id=?', (text, cid))
            context.user_data.clear()
            await msg.reply_text('✅ Đã cập nhật link hướng dẫn cách cài cho sản phẩm thành công!', reply_markup=ADMIN_KB)
        except Exception:
            log.exception('Save guide url failed')
            context.user_data.clear()
            await msg.reply_text('❌ Lỗi khi lưu link hướng dẫn.', reply_markup=ADMIN_KB)
        return

    if flow == 'broadcast_content' and is_admin(update):
        photo_file_id = None
        if msg.photo:
            photo_file_id = msg.photo[-1].file_id
            bc_text = msg.caption or ''
        else:
            bc_text = text

        context.user_data['pending_broadcast'] = {
            'text': bc_text,
            'photo': photo_file_id
        }
        context.user_data['flow'] = None

        preview_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('✅ Xác nhận gửi', callback_data='adm:broadcast:confirm'),
             InlineKeyboardButton('❌ Hủy', callback_data='adm:broadcast:cancel')]
        ])
        
        if photo_file_id:
            await msg.reply_photo(photo=photo_file_id, caption=f"📢 **XEM TRƯỚC THÔNG BÁO:**\n\n{bc_text}", parse_mode='HTML', reply_markup=preview_kb)
        else:
            await msg.reply_text(f"📢 **XEM TRƯỚC THÔNG BÁO:**\n\n{bc_text}", parse_mode='HTML', reply_markup=preview_kb)
        return

    if flow == 'custom_quantity':
        if not text.isdigit() or not 1 <= int(text) <= 1000: await msg.reply_text('Nhập số nguyên từ 1 đến 1.000.'); return
        oid = context.user_data.get('selected_option'); context.user_data['flow'] = None
        with db() as con:
            o = con.execute('SELECT po.*,p.name AS product_name,c.name AS category_name FROM product_options po JOIN products p ON p.id::text=po.product_id::text JOIN categories c ON c.id::text=p.category_id::text WHERE po.id=?', (oid,)).fetchone()
            u_row = con.execute('SELECT is_seller FROM users WHERE user_id=?',(user.id,)).fetchone()
        if not o: await msg.reply_text('Gói sản phẩm không còn tồn tại.', reply_markup=USER_KB); return
        is_seller = bool(u_row and u_row['is_seller'])
        current_price = o['sell_price'] if (is_seller and o['sell_price'] > 0) else o['price']
        qty = int(text); context.user_data['pending_purchase'] = {'option_id': oid, 'quantity': qty, 'total': current_price * qty}
        p = context.user_data['pending_purchase']
        payment_note = ('ShopHenry sẽ tự trừ số dư tài khoản nguồn; bot không trừ ví nội bộ.' if o.get('shophenry_product_id') else 'Bot sẽ kiểm tra kho và số dư trước khi trừ tiền.')
        await msg.reply_text(f"🧾 XÁC NHẬN MUA HÀNG\n\n📁 Danh mục: {escape(o['category_name'])}\n📦 Sản phẩm: {escape(o['product_name'])}\n⏳ Thời hạn: {escape(o['duration_label'])}\n🔢 Số lượng: {p['quantity']}\n💰 Giá bán: {p['total']:,}đ\n\n{payment_note}", parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('✅ XÁC NHẬN MUA', callback_data='shop:confirm')], [InlineKeyboardButton('❌ HỦY', callback_data='shop:cancel')], [InlineKeyboardButton('↩️ Trở về', callback_data='menu:home')]])); return
    if flow == 'deposit_amount':
        if not text.isdigit() or not 1 <= int(text) <= 1_000_000_000:
            await msg.reply_text('Số tiền không hợp lệ. Nhập số nguyên từ 1 đến 1.000.000.000 VNĐ:'); return
        amount = int(text); uid = user.id
        try:
            with db() as con:
                row = con.execute('SELECT last_deposit_request_at,is_locked,username,full_name FROM users WHERE user_id=? FOR UPDATE', (uid,)).fetchone()
                if not row or row['is_locked']:
                    await msg.reply_text('⛔ Tài khoản không khả dụng.'); return
                last = row['last_deposit_request_at']
                if last:
                    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
                    if elapsed < 30:
                        await msg.reply_text(f'⏳ Bạn cần chờ {max(1, int(30 - elapsed))} giây nữa mới tạo được đơn nạp mới.'); return
                dep = con.execute("INSERT INTO deposits(user_id,amount,status,note,created_at) VALUES(?,?,'pending',NULL,?) RETURNING id", (uid, amount, now())).fetchone()
                deposit_id = dep['id']; memo = f'{QR_MEMO_PREFIX}{uid}{QR_MEMO_SUFFIX}{deposit_id}'
                con.execute('UPDATE deposits SET note=? WHERE id=?', (memo, deposit_id))
                con.execute('UPDATE users SET last_deposit_request_at=? WHERE user_id=?', (datetime.now(timezone.utc), uid))
            context.user_data['flow'] = None
            qr_url = make_qr_url(amount, memo)
            caption = (f'💳 THÔNG TIN NẠP TIỀN\n\n🧾 Mã đơn: #{deposit_id}\n💰 Số tiền: {amount:,}đ\n🏦 Ngân hàng: {QR_BANK}\n📝 Nội dung chuyển khoản: {memo}\n\nQuét mã QR để chuyển khoản đúng số tiền và nội dung. Sau khi chuyển xong, bấm “Đã chuyển tiền”.')
            kb = InlineKeyboardMarkup([[InlineKeyboardButton('✅ Đã chuyển tiền', callback_data=f'deposit:submitted:{deposit_id}')], [InlineKeyboardButton('❌ Hủy', callback_data=f'deposit:cancel:{deposit_id}')]])
            await msg.reply_photo(photo=qr_url, caption=caption, reply_markup=kb)
        except Exception:
            log.exception('Create deposit request failed')
            await msg.reply_text('⚠️ Không tạo được mã QR lúc này. Vui lòng thử lại sau.')
        return
    if flow == 'admin_balance_amount' and is_admin(update):
        if not text.isdigit() or not 1 <= int(text) <= 1_000_000_000:
            await msg.reply_text('Nhập số tiền nguyên dương, tối đa 1.000.000.000 VNĐ:'); return
        amount = int(text); uid = int(context.user_data.get('target_user_id') or 0); action = context.user_data.get('balance_action')
        try:
            with db() as con:
                row = con.execute('SELECT balance FROM users WHERE user_id=? FOR UPDATE', (uid,)).fetchone()
                if not row:
                    context.user_data.clear(); await msg.reply_text('Không tìm thấy người dùng.', reply_markup=ADMIN_KB); return
                if action == 'subtract' and row['balance'] < amount:
                    await msg.reply_text(f"Số dư hiện tại chỉ có {row['balance']:,}đ. Nhập số tiền nhỏ hơn hoặc bằng số dư:"); return
                if action == 'add':
                    con.execute('UPDATE users SET balance=balance+? WHERE user_id=?', (amount, uid))
                else:
                    con.execute('UPDATE users SET balance=balance-? WHERE user_id=?', (amount, uid))
                after = con.execute('SELECT balance FROM users WHERE user_id=?', (uid,)).fetchone()['balance']
                con.execute('INSERT INTO balance_transactions(user_id,admin_id,action,amount,balance_after,note,created_at) VALUES(?,?,?,?,?,?,?)', (uid, user.id, action, amount, after, 'Admin điều chỉnh số dư', now()))
            context.user_data.clear()
            verb = 'cộng' if action == 'add' else 'trừ'
            await msg.reply_text(f'✅ Đã {verb} {amount:,}đ cho người dùng {uid}.\nSố dư mới: {after:,}đ.', reply_markup=ADMIN_KB)
            try: await context.bot.send_message(chat_id=uid, text=f'💰 Admin đã {verb} {amount:,}đ vào số dư của bạn. Số dư hiện tại: {after:,}đ.')
            except Exception: pass
        except Exception:
            log.exception('Admin balance adjustment failed')
            await msg.reply_text('⚠️ Không thể điều chỉnh số dư. Kiểm tra Logs.', reply_markup=ADMIN_KB)
        return
    if flow == 'rename_category' and is_admin(update):
        cid = context.user_data.get('rename_category_id')
        try:
            with db() as con: con.execute('UPDATE categories SET name=? WHERE id=?', (text, cid))
        except Exception:
            log.exception('Rename category failed'); await msg.reply_text('Tên đã tồn tại hoặc không thể cập nhật.'); return
        context.user_data.clear(); await msg.reply_text('✅ Đã đổi tên danh mục ngoài.', reply_markup=ADMIN_KB); return
    if flow == 'edit_option_price' and is_admin(update):
        if not text.isdigit() or int(text) < 0:
            await msg.reply_text('Giá phải là số nguyên không âm. Nhập lại giá thường VNĐ:'); return
        oid = context.user_data.get('edit_option_id')
        with db() as con: con.execute('UPDATE product_options SET price=? WHERE id=?', (int(text), oid))
        context.user_data.clear(); await msg.reply_text('✅ Đã cập nhật giá thường thành công.', reply_markup=ADMIN_KB); return
    if flow == 'edit_sell_price_val' and is_admin(update):
        if not text.isdigit() or int(text) < 0:
            await msg.reply_text('Giá Sell phải là số nguyên không âm. Nhập lại:'); return
        oid = context.user_data.get('edit_option_id')
        with db() as con: con.execute('UPDATE product_options SET sell_price=? WHERE id=?', (int(text), oid))
        context.user_data.clear(); await msg.reply_text('✅ Đã cập nhật mức giá Sell thành công.', reply_markup=ADMIN_KB); return
    if flow == 'add_category_name' and is_admin(update):
        with db() as con: con.execute('INSERT INTO categories(name,created_by,created_at) VALUES(?,?,?) ON CONFLICT(name) DO NOTHING', (text, user.id, now()))
        context.user_data.clear(); await msg.reply_text('✅ Đã thêm danh mục ngoài.', reply_markup=ADMIN_KB); return
    if flow == 'add_product_name' and is_admin(update):
        cid = context.user_data.get('new_product_category')
        if not cid:
            await msg.reply_text('⚠️ Bạn chưa chọn danh mục. Vui lòng bấm /admin để thực hiện lại.', reply_markup=ADMIN_KB)
            context.user_data.clear(); return
        if not text or len(text) > 100:
            await msg.reply_text('Tên sản phẩm không hợp lệ. Nhập lại (tối đa 100 ký tự):'); return
        try:
            with db() as con:
                p = con.execute('INSERT INTO products(category_id,name,created_by,created_at) VALUES(?,?,?,?) RETURNING id', (cid, text, user.id, now())).fetchone()
                con.execute("INSERT INTO product_options(product_id,duration,duration_label,price,sell_price) VALUES(?,'1h','1 giờ',0,0),(?,'1d','1 ngày',0,0),(?,'7d','7 ngày',0,0),(?,'30d','30 ngày',0,0)", (p['id'], p['id'], p['id'], p['id']))
            context.user_data.clear()
            await msg.reply_text(f'✅ Thêm sản phẩm "{text}" thành công!\n\nĐã tạo 4 gói thời hạn.', reply_markup=ADMIN_KB)
        except Exception:
            log.exception('Add product failed')
            context.user_data.clear(); await msg.reply_text('❌ Không thể thêm sản phẩm. Kiểm tra Logs.', reply_markup=ADMIN_KB)
        return
    if flow == 'map_shophenry_id' and is_admin(update):
        if not text.isdigit():
            await msg.reply_text('ID ShopHenry phải là số. Nhập lại product_id:'); return
        oid = context.user_data.get('shophenry_target_option')
        try:
            with db() as con: con.execute('UPDATE product_options SET shophenry_product_id=? WHERE id=?', (text, oid))
            context.user_data.clear(); await msg.reply_text(f'✅ Đã gán ID ShopHenry {text} cho gói sản phẩm.', reply_markup=ADMIN_KB)
        except Exception:
            log.exception('Map ShopHenry option failed'); context.user_data.clear()
            await msg.reply_text('❌ Không thể gán ID ShopHenry. Kiểm tra Logs.', reply_markup=ADMIN_KB)
        return
    if flow == 'user_check_id' and is_admin(update):
        with db() as con:
            if text.startswith('@') or not text.isdigit():
                handle = text.lstrip('@').strip()
                r = con.execute('SELECT * FROM users WHERE LOWER(username)=LOWER(?) ORDER BY last_seen DESC LIMIT 1', (handle,)).fetchone()
            else:
                r = con.execute('SELECT * FROM users WHERE user_id=?', (int(text),)).fetchone()
        context.user_data.clear()
        if not r: await msg.reply_text('Không tìm thấy người dùng. Kiểm tra ID hoặc @username.', reply_markup=ADMIN_KB); return
        await show_admin_user(msg, r['user_id']); return
    if flow == 'add_product_keys' and is_admin(update):
        oid = context.user_data.get('key_option_id'); keys = clean_lines(text); added = 0
        if not keys: await msg.reply_text('Không tìm thấy key hợp lệ. Gửi lại mỗi key một dòng.'); return
        with db() as con:
            for key in keys:
                cur = con.execute("INSERT INTO product_keys(option_id,key_value,added_by,added_at) VALUES(?,?,?,?) ON CONFLICT(key_value) DO NOTHING", (oid, key, user.id, now())); added += cur.rowcount
            info = con.execute('SELECT p.name AS product_name,c.name AS category_name,po.duration_label FROM product_options po JOIN products p ON p.id::text=po.product_id::text JOIN categories c ON c.id::text=p.category_id::text WHERE po.id=?', (oid,)).fetchone()
            stock = con.execute("SELECT COUNT(*) AS n FROM product_keys WHERE option_id=? AND status='available'", (oid,)).fetchone()['n']
        context.user_data.clear()
        await msg.reply_text(f'✅ Đã thêm {added}/{len(keys)} key thành công. Kho hiện có: {stock} key.', reply_markup=ADMIN_KB)
        if added:
            announcement = (f"🔥 CÓ HÀNG MỚI VỀ! 🔥\n━━━━━━━━━━━━━━━━━━\n📁 Danh mục: {info['category_name']}\n🎮 Sản phẩm: {info['product_name']}\n🔢 Số lượng: {added} sản phẩm đã được cập nhật!\n\n👉 Nhấn nút Mở Shop bên dưới để vào mua ngay!")
            shop_markup = InlineKeyboardMarkup([[InlineKeyboardButton('🛍 MỞ SHOP', web_app=WebAppInfo(url=MINI_APP_URL))]]) if MINI_APP_URL.startswith('https://') else None
            sent = await broadcast(context.bot, announcement, reply_markup=shop_markup)
            await msg.reply_text(f'📢 Đã thông báo tới {sent} người dùng.')
        return
    
    if is_admin(update):
        await msg.reply_text('🛠 Bạn đang không ở luồng nhập liệu nào. Vui lòng bấm /admin để mở bảng quản trị.', reply_markup=ADMIN_KB)
    else:
        await msg.reply_text('Vui lòng gửi /start để mở menu chính.')

async def error_handler(update, context): log.exception('Unhandled error', exc_info=context.error)

# ---------------- MINI APP WEB + API ----------------
def _telegram_http(method, payload=None):
    if not BOT_TOKEN:
        raise RuntimeError('Thiếu BOT_TOKEN')
    url=f'https://api.telegram.org/bot{BOT_TOKEN}/{method}'
    body=json.dumps(payload or {}).encode('utf-8')
    req=urllib.request.Request(url,data=body,headers={'Content-Type':'application/json'},method='POST')
    with urllib.request.urlopen(req,timeout=25) as response:
        result=json.loads(response.read().decode('utf-8'))
    if not result.get('ok'):
        raise RuntimeError(str(result.get('description') or 'Telegram API error'))
    return result.get('result')


def _telegram_init_user(request: Request):
    raw = request.headers.get('X-Telegram-Init-Data', '')
    if not raw or not BOT_TOKEN:
        raise HTTPException(status_code=401, detail='Thiếu dữ liệu xác thực Telegram. Hãy mở Mini App từ bot.')
    try:
        pairs = dict(parse_qsl(raw, keep_blank_values=True))
        supplied_hash = pairs.pop('hash', '')
        auth_date = int(pairs.get('auth_date', '0'))
        if not supplied_hash or not auth_date or time.time() - auth_date > 86400 or auth_date > time.time() + 60:
            raise ValueError('expired')
        check_string = '\n'.join(f'{k}={v}' for k, v in sorted(pairs.items()))
        secret = hmac.new(b'WebAppData', BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, supplied_hash):
            raise ValueError('invalid hash')
        user = json.loads(pairs['user'])
        uid = int(user['id'])
        return {'id': uid, 'username': user.get('username') or '',
                'full_name': ' '.join(x for x in [user.get('first_name'), user.get('last_name')] if x).strip()}
    except Exception:
        raise HTTPException(status_code=401, detail='Dữ liệu Telegram không hợp lệ hoặc đã hết hạn. Hãy đóng và mở lại Mini App.')


def _mini_user(request: Request, require_admin=False):
    user = _telegram_init_user(request)
    if require_admin and user['id'] not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail='Bạn không có quyền Admin.')
    stamp = now()
    with db() as con:
        con.execute('''INSERT INTO users(user_id,username,full_name,joined_at,last_seen)
            VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
            username=EXCLUDED.username, full_name=EXCLUDED.full_name,last_seen=EXCLUDED.last_seen''',
            (user['id'], user['username'], user['full_name'], stamp, stamp))
        row = con.execute('SELECT is_locked FROM users WHERE user_id=?', (user['id'],)).fetchone()
    if row and row['is_locked'] and user['id'] not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail='Tài khoản của bạn đang bị khóa.')
    if maintenance_enabled() and user['id'] not in ADMIN_IDS:
        raise HTTPException(status_code=503, detail='Bot đang bảo trì. Vui lòng quay lại sau.')
    return user


@web_app.get('/')
async def miniapp_index():
    html_path = BASE_DIR / 'index.html'
    if not html_path.exists():
        html_path = BASE_DIR / 'index(3).html'
    if not html_path.exists():
        raise HTTPException(status_code=404, detail='Không tìm thấy index.html hoặc index(3).html')
    return FileResponse(html_path, media_type='text/html; charset=utf-8')


@web_app.get('/health')
async def health():
    return {'ok': True, 'service': 'jinny-cheat'}


@web_app.get('/api/me')
async def api_me(request: Request):
    user = _mini_user(request)
    with db() as con:
        u = con.execute('SELECT balance,is_locked,is_seller FROM users WHERE user_id=?', (user['id'],)).fetchone()
        deposits = con.execute("SELECT id,amount,status,note,created_at FROM deposits WHERE user_id=? AND status='approved' ORDER BY id DESC LIMIT 30", (user['id'],)).fetchall()
        purchases = con.execute('SELECT id,product_name,duration_label,quantity,total_price,keys_text,created_at FROM purchases WHERE user_id=? ORDER BY id DESC LIMIT 30', (user['id'],)).fetchall()
        total_deposit = con.execute("SELECT COALESCE(SUM(amount),0) AS n FROM deposits WHERE user_id=? AND status='approved'", (user['id'],)).fetchone()['n']
        total_spent = con.execute('SELECT COALESCE(SUM(total_price),0) AS n FROM purchases WHERE user_id=?', (user['id'],)).fetchone()['n']
        icon_row=con.execute("SELECT setting_value FROM bot_settings WHERE setting_key='miniapp_ff_icon'").fetchone()
    return {'user_id': user['id'], 'balance': int(u['balance']), 'is_admin': user['id'] in ADMIN_IDS,
            'is_locked': bool(u['is_locked']), 'is_seller': bool(u['is_seller']),
            'ff_app_icon': icon_row['setting_value'] if icon_row else '',
            'total_deposit': int(total_deposit), 'total_spent': int(total_spent),
            'deposits': deposits, 'purchases': purchases}


@web_app.get('/api/catalog')
async def api_catalog(request: Request):
    user = _mini_user(request)
    with db() as con:
        cats = con.execute('SELECT id,name,guide_url FROM categories ORDER BY id').fetchall()
        output=[]
        seller = con.execute('SELECT is_seller FROM users WHERE user_id=?', (user['id'],)).fetchone()
        for cat in cats:
            products = con.execute('SELECT id,name FROM products WHERE category_id=? ORDER BY id', (cat['id'],)).fetchall()
            prod_out=[]
            for product in products:
                opts=con.execute('''SELECT po.id,po.duration,po.duration_label,po.price,po.sell_price,
                    po.shophenry_product_id,COUNT(pk.id) FILTER (WHERE pk.status='available') AS stock
                    FROM product_options po LEFT JOIN product_keys pk ON pk.option_id=po.id
                    WHERE po.product_id=? GROUP BY po.id ORDER BY po.id''',(product['id'],)).fetchall()
                prod_out.append({'id':product['id'],'name':product['name'],'options':[{
                    'id':o['id'],'duration':o['duration'],'duration_label':o['duration_label'],
                    'price':int(o['sell_price'] if seller and seller['is_seller'] and o['sell_price'] else o['price']),
                    'stock':int(o['stock']),'shophenry':bool(o['shophenry_product_id'])} for o in opts]})
            output.append({'id':cat['id'],'name':cat['name'],'guide_url':cat['guide_url'],'products':prod_out})
    return {'categories':output}


@web_app.post('/api/purchases')
async def api_purchase(request: Request):
    user = _mini_user(request)
    payload = await request.json()
    try:
        oid=int(payload.get('option_id')); qty=int(payload.get('quantity'))
        if qty < 1 or qty > 100: raise ValueError()
    except Exception:
        raise HTTPException(status_code=400, detail='Số lượng hoặc gói sản phẩm không hợp lệ.')
    supplier_order=''; key_text=''; con=db()
    try:
        con.execute('BEGIN')
        u=con.execute('SELECT balance,is_locked,is_seller FROM users WHERE user_id=? FOR UPDATE',(user['id'],)).fetchone()
        opt=con.execute('''SELECT po.*,p.name AS product_name,c.name AS category_name FROM product_options po
            JOIN products p ON p.id=po.product_id JOIN categories c ON c.id=p.category_id WHERE po.id=?''',(oid,)).fetchone()
        if not u or u['is_locked']: raise HTTPException(status_code=403,detail='Tài khoản không thể mua hàng.')
        if not opt: raise HTTPException(status_code=404,detail='Gói sản phẩm không tồn tại.')
        price=opt['sell_price'] if u['is_seller'] and opt['sell_price']>0 else opt['price']; total=int(price)*qty
        external=is_aim_hack_product(opt['product_name'])
        if external:
            if not opt['shophenry_product_id']: raise HTTPException(status_code=400,detail='Sản phẩm Aim Hack chưa được gán ID ShopHenry.')
            con.rollback()
            key_text,supplier_order=await asyncio.to_thread(shophenry_buy,opt['shophenry_product_id'],qty)
            con.execute('BEGIN')
            u=con.execute('SELECT balance,is_locked,is_seller FROM users WHERE user_id=? FOR UPDATE',(user['id'],)).fetchone()
        else:
            if u['balance'] < total: raise HTTPException(status_code=400,detail=f'Số dư không đủ. Cần {total:,}đ, hiện có {u["balance"]:,}đ.')
            keys=con.execute("SELECT id,key_value FROM product_keys WHERE option_id=? AND status='available' ORDER BY id LIMIT ? FOR UPDATE SKIP LOCKED",(oid,qty)).fetchall()
            if len(keys)<qty: raise HTTPException(status_code=400,detail=f'Kho không đủ key. Hiện còn {len(keys)} key.')
            key_text='\n'.join(k['key_value'] for k in keys)
            con.execute('UPDATE users SET balance=balance-? WHERE user_id=?',(total,user['id']))
            for k in keys: con.execute("UPDATE product_keys SET status='sold',sold_to=?,sold_at=? WHERE id=?",(user['id'],now(),k['id']))
        purchase_row=con.execute('''INSERT INTO purchases(user_id,category_name,product_name,duration_label,quantity,unit_price,total_price,keys_text,created_at,supplier_order_code)
            VALUES(?,?,?,?,?,?,?,?,?,?) RETURNING id''',(user['id'],opt['category_name'],opt['product_name'],opt['duration_label'],qty,price,total,key_text,now(),supplier_order or None)).fetchone()
        purchase_id=int(purchase_row['id'])
        con.commit()
    except HTTPException:
        con.rollback(); raise
    except Exception as e:
        con.rollback(); log.exception('Mini App purchase failed'); raise HTTPException(status_code=500,detail=str(e)[:500])
    finally: con.close()
    try:
        await asyncio.to_thread(_telegram_http,'sendMessage',{'chat_id':user['id'],'text':'🔑 KEY CỦA BẠN:\n'+key_text})
    except Exception: log.exception('Could not send purchased keys to Telegram user %s',user['id'])
    return {'success':True,'purchase_id':purchase_id,'total':total,'total_price':total,'keys_text':key_text,'message':'Mua hàng thành công. Key đã được gửi vào chat bot.'}


@web_app.post('/api/deposits')
async def api_create_deposit(request: Request):
    user=_mini_user(request); payload=await request.json()
    try: amount=int(payload.get('amount'))
    except Exception: raise HTTPException(status_code=400,detail='Số tiền không hợp lệ.')
    if amount<2000 or amount>1000000000: raise HTTPException(status_code=400,detail='Số tiền nạp từ 2.000đ đến 1.000.000.000đ.')
    with db() as con:
        # Serialize concurrent create requests for the same account.
        con.execute('SELECT user_id FROM users WHERE user_id=? FOR UPDATE',(user['id'],)).fetchone()
        con.execute("""UPDATE deposits SET status='expired'
            WHERE user_id=? AND status='pending'
              AND created_at::timestamptz < NOW() - INTERVAL '5 minutes'""",(user['id'],))
        recent=con.execute("SELECT id,status FROM deposits WHERE user_id=? AND status IN ('pending','submitted') ORDER BY id DESC LIMIT 1",(user['id'],)).fetchone()
        if recent:
            message = ('Bot đang xét duyệt đơn nạp tiền của bạn. Vui lòng không gửi spam yêu cầu cho Admin.'
                       if recent['status']=='submitted' else 'Bạn đang có mã nạp tiền còn hiệu lực. Hãy dùng mã hiện tại hoặc đợi mã hết hạn.')
            raise HTTPException(status_code=409,detail=message)
        row=con.execute("INSERT INTO deposits(user_id,amount,status,note,created_at) VALUES(?,?,'pending','',?) RETURNING id",(user['id'],amount,now())).fetchone()
        did=int(row['id']); memo=f'{QR_MEMO_SUFFIX}{user["id"]}D{did}'
        con.execute('UPDATE deposits SET note=? WHERE id=?',(memo,did))
    return {'deposit_id':did,'amount':amount,'transfer_content':memo,'qr_url':make_qr_url(amount,memo),'expires_in':300}


@web_app.post('/api/deposits/{deposit_id}/submitted')
async def api_submit_deposit(deposit_id: int, request: Request):
    user=_mini_user(request)
    with db() as con:
        dep=con.execute('SELECT d.*,u.full_name,u.username FROM deposits d JOIN users u ON u.user_id=d.user_id WHERE d.id=? AND d.user_id=? FOR UPDATE',(deposit_id,user['id'])).fetchone()
        if not dep: raise HTTPException(status_code=404,detail='Không tìm thấy đơn nạp.')
        if dep['status']=='submitted':
            return {'success':True,'already_submitted':True,'message':'Bot đang xét duyệt đơn nạp tiền của bạn. Vui lòng không gửi spam yêu cầu cho Admin.'}
        if dep['status']!='pending': raise HTTPException(status_code=400,detail='Đơn nạp đã được xử lý hoặc hết hạn.')
        con.execute("UPDATE deposits SET status='submitted' WHERE id=?",(deposit_id,))
    body=(f'💳 YÊU CẦU XÁC NHẬN NẠP TIỀN\n\n🧾 Mã đơn: #{deposit_id}\n💰 Số tiền: {dep["amount"]:,}đ\n📝 Nội dung CK: {dep["note"]}\n👤 Tên: {dep["full_name"] or "Không có"}\n🆔 ID: {user["id"]}\n📛 Username: @{dep["username"] or "Không có"}\n\nKiểm tra giao dịch ngân hàng trước khi duyệt.')
    kb={'inline_keyboard':[[{'text':'✅ DUYỆT','callback_data':f'deposit:approve:{deposit_id}'},{'text':'❌ TỪ CHỐI','callback_data':f'deposit:reject:{deposit_id}'}]]}
    for aid in ADMIN_IDS:
        try: await asyncio.to_thread(_telegram_http,'sendMessage',{'chat_id':aid,'text':body,'reply_markup':kb})
        except Exception: log.exception('Cannot notify admin for deposit %s',deposit_id)
    return {'success':True,'message':'Đã gửi yêu cầu một lần. Bot đang xét duyệt đơn nạp tiền của bạn. Vui lòng không gửi spam yêu cầu cho Admin.'}


@web_app.get('/api/admin/overview')
async def api_admin_overview(request: Request):
    _mini_user(request,True)
    with db() as con:
        cats=con.execute('SELECT id,name FROM categories ORDER BY id').fetchall()
        products=con.execute('SELECT p.id,p.name,p.category_id,c.name AS category_name FROM products p JOIN categories c ON c.id=p.category_id ORDER BY p.id').fetchall()
        options=con.execute('SELECT po.id,po.product_id,po.duration_label,po.price,po.sell_price,po.shophenry_product_id,p.name AS product_name FROM product_options po JOIN products p ON p.id=po.product_id ORDER BY po.id').fetchall()
        users=con.execute('SELECT COUNT(*) AS n FROM users').fetchone()['n']
        revenue=con.execute("SELECT COALESCE(SUM(amount),0) AS n FROM deposits WHERE status='approved'").fetchone()['n']
        balances=con.execute('SELECT COALESCE(SUM(balance),0) AS n FROM users').fetchone()['n']
        maint=con.execute("SELECT setting_value FROM bot_settings WHERE setting_key='maintenance'").fetchone()
    return {'categories':cats,'products':products,'options':options,'stats':{'users':users,'revenue':int(revenue),'balances':int(balances)},'maintenance':bool(maint and maint['setting_value']=='on')}


@web_app.post('/api/admin/action')
async def api_admin_action(request: Request):
    admin=_mini_user(request,True); data=await request.json(); action=str(data.get('action',''))
    try:
        with db() as con:
            if action=='set_ff_app_icon':
                image_data=str(data.get('image_data') or '')
                if not image_data.startswith('data:image/') or len(image_data)>2_500_000:
                    raise HTTPException(status_code=400,detail='Ảnh không hợp lệ hoặc vượt quá dung lượng cho phép.')
                con.execute("""INSERT INTO bot_settings(setting_key,setting_value) VALUES('miniapp_ff_icon',?)
                    ON CONFLICT(setting_key) DO UPDATE SET setting_value=EXCLUDED.setting_value""",(image_data,))
                return {'success':True,'message':'Đã lưu ảnh Free Fire. Ảnh sẽ hiển thị cho tất cả người dùng.'}
            if action=='stats':
                return {'stats':con.execute("SELECT (SELECT COUNT(*) FROM users) AS users,(SELECT COALESCE(SUM(amount),0) FROM deposits WHERE status='approved') AS deposits,(SELECT COALESCE(SUM(balance),0) FROM users) AS balances,(SELECT COUNT(*) FROM purchases) AS purchases").fetchone()}
            if action=='usercheck':
                target=str(data.get('query','')).strip().lstrip('@')
                row=con.execute('SELECT user_id,username,full_name,balance,is_locked,is_seller,joined_at,last_seen FROM users WHERE user_id::text=? OR LOWER(username)=LOWER(?) ORDER BY last_seen DESC LIMIT 1',(target,target)).fetchone()
                if not row: raise HTTPException(status_code=404,detail='Không tìm thấy người dùng.')
                return {'user':row}
            if action in ('lock_user','unlock_user','set_sell','remove_sell'):
                uid=int(data.get('user_id')); col='is_locked' if action in ('lock_user','unlock_user') else 'is_seller'; val=action in ('lock_user','set_sell')
                cur=con.execute(f'UPDATE users SET {col}=? WHERE user_id=?',(val,uid))
                if not cur.rowcount: raise HTTPException(status_code=404,detail='Không tìm thấy người dùng.')
                return {'success':True,'message':'Đã cập nhật tài khoản.'}
            if action in ('add_balance','subtract_balance'):
                uid=int(data.get('user_id')); amount=int(data.get('amount'))
                if amount<1 or amount>1000000000: raise HTTPException(status_code=400,detail='Số tiền không hợp lệ.')
                row=con.execute('SELECT balance FROM users WHERE user_id=? FOR UPDATE',(uid,)).fetchone()
                if not row: raise HTTPException(status_code=404,detail='Không tìm thấy người dùng.')
                if action=='subtract_balance' and row['balance']<amount: raise HTTPException(status_code=400,detail='Số dư không đủ.')
                delta=amount if action=='add_balance' else -amount
                con.execute('UPDATE users SET balance=balance+? WHERE user_id=?',(delta,uid))
                after=con.execute('SELECT balance FROM users WHERE user_id=?',(uid,)).fetchone()['balance']
                con.execute('INSERT INTO balance_transactions(user_id,admin_id,action,amount,balance_after,note,created_at) VALUES(?,?,?,?,?,?,?)',(uid,admin['id'],'add' if delta>0 else 'subtract',amount,after,'Điều chỉnh từ Mini App Admin',now()))
                return {'success':True,'balance':after}
            if action=='user_history':
                uid=int(data.get('user_id')); kind=data.get('kind')
                if kind=='deposits': rows=con.execute('SELECT id,amount,status,note,created_at FROM deposits WHERE user_id=? ORDER BY id DESC LIMIT 20',(uid,)).fetchall()
                else: rows=con.execute('SELECT id,product_name,duration_label,quantity,total_price,created_at FROM purchases WHERE user_id=? ORDER BY id DESC LIMIT 20',(uid,)).fetchall()
                return {'items':rows}
            if action=='add_category':
                name=str(data.get('name','')).strip()
                if not name or len(name)>80: raise HTTPException(status_code=400,detail='Tên danh mục không hợp lệ.')
                con.execute('INSERT INTO categories(name,created_by,created_at) VALUES(?,?,?)',(name,admin['id'],now())); return {'success':True,'message':'Đã thêm danh mục.'}
            if action=='add_product':
                cid=int(data.get('category_id')); name=str(data.get('name','')).strip()
                if not name or len(name)>100: raise HTTPException(status_code=400,detail='Tên sản phẩm không hợp lệ.')
                row=con.execute('INSERT INTO products(category_id,name,created_by,created_at) VALUES(?,?,?,?) RETURNING id',(cid,name,admin['id'],now())).fetchone(); pid=row['id']
                for dur,label in [('1h','1 giờ'),('1d','1 ngày'),('7d','7 ngày'),('30d','30 ngày')]: con.execute('INSERT INTO product_options(product_id,duration,duration_label,price,sell_price) VALUES(?,?,?,?,?)',(pid,dur,label,0,0))
                return {'success':True,'message':f'Đã tạo sản phẩm {name} cùng 4 gói thời hạn.'}
            if action=='add_guide':
                cid=int(data.get('category_id')); url=str(data.get('url','')).strip()
                if not url.startswith(('https://','http://')): raise HTTPException(status_code=400,detail='Link phải bắt đầu bằng http:// hoặc https://')
                con.execute('UPDATE categories SET guide_url=? WHERE id=?',(url,cid)); return {'success':True,'message':'Đã lưu link hướng dẫn.'}
            if action=='map_shop':
                oid=int(data.get('option_id')); sid=str(data.get('shophenry_product_id','')).strip()
                if not sid.isdigit(): raise HTTPException(status_code=400,detail='ShopHenry product ID phải là số.')
                con.execute('UPDATE product_options SET shophenry_product_id=? WHERE id=?',(sid,oid)); return {'success':True,'message':'Đã gán ID ShopHenry.'}
            if action in ('edit_price','edit_sell_price'):
                oid=int(data.get('option_id')); price=int(data.get('price'))
                if price<0: raise HTTPException(status_code=400,detail='Giá không được âm.')
                col='price' if action=='edit_price' else 'sell_price'; con.execute(f'UPDATE product_options SET {col}=? WHERE id=?',(price,oid)); return {'success':True,'message':'Đã cập nhật giá.'}
            if action=='rename_category':
                cid=int(data.get('category_id')); name=str(data.get('name','')).strip()
                if not name: raise HTTPException(status_code=400,detail='Tên mới không được trống.')
                con.execute('UPDATE categories SET name=? WHERE id=?',(name,cid)); return {'success':True,'message':'Đã đổi tên danh mục.'}
            if action in ('delete_category','delete_product'):
                ident=int(data.get('id')); table='categories' if action=='delete_category' else 'products'
                cur=con.execute(f'DELETE FROM {table} WHERE id=?',(ident,))
                if not cur.rowcount: raise HTTPException(status_code=404,detail='Không tìm thấy mục cần xóa.')
                return {'success':True,'message':'Đã xóa. Các dữ liệu con liên quan sẽ được xử lý theo ràng buộc database.'}
            if action=='add_keys':
                oid=int(data.get('option_id')); keys=clean_lines(str(data.get('keys','')))
                if not keys: raise HTTPException(status_code=400,detail='Không có key hợp lệ.')
                added=0
                for key in keys:
                    cur=con.execute("INSERT INTO product_keys(option_id,key_value,added_by,added_at) VALUES(?,?,?,?) ON CONFLICT(key_value) DO NOTHING",(oid,key,admin['id'],now())); added+=cur.rowcount
                stock=con.execute("SELECT COUNT(*) AS n FROM product_keys WHERE option_id=? AND status='available'",(oid,)).fetchone()['n']
                return {'success':True,'message':f'Đã thêm {added}/{len(keys)} key. Kho hiện có {stock} key.'}
            if action=='stock':
                pid=int(data.get('product_id'))
                rows=con.execute("SELECT po.id,po.duration_label,po.price,po.sell_price,COUNT(pk.id) FILTER (WHERE pk.status='available') AS available,COUNT(pk.id) AS total FROM product_options po LEFT JOIN product_keys pk ON pk.option_id=po.id WHERE po.product_id=? GROUP BY po.id ORDER BY po.id",(pid,)).fetchall()
                return {'items':rows}
            if action=='maintenance':
                current=con.execute("SELECT setting_value FROM bot_settings WHERE setting_key='maintenance'").fetchone(); new='off' if current and current['setting_value']=='on' else 'on'
                con.execute("INSERT INTO bot_settings(setting_key,setting_value,updated_at) VALUES('maintenance',?,?) ON CONFLICT(setting_key) DO UPDATE SET setting_value=EXCLUDED.setting_value,updated_at=EXCLUDED.updated_at",(new,now()))
                return {'success':True,'maintenance':new=='on','message':'Đã bật bảo trì.' if new=='on' else 'Đã tắt bảo trì.'}
            if action=='broadcast':
                message=str(data.get('message','')).strip()
                if not message or len(message)>4000: raise HTTPException(status_code=400,detail='Nội dung thông báo trống hoặc quá dài.')
                ids=[r['user_id'] for r in con.execute('SELECT user_id FROM users WHERE is_locked=FALSE ORDER BY user_id').fetchall()]
            else: ids=[]
        if action=='broadcast':
            sent=0; failed=0
            for uid in ids:
                try: await asyncio.to_thread(_telegram_http,'sendMessage',{'chat_id':uid,'text':message}); sent+=1
                except Exception: failed+=1
                await asyncio.sleep(0.04)
            return {'success':True,'message':f'Đã gửi {sent}/{len(ids)} người dùng; lỗi {failed}.'}
        if action=='emoji':
            stickers=await asyncio.to_thread(_telegram_http,'getStickerSet',{'name':'NewsEmoji'})
            return {'items':[{'emoji':x.get('emoji') or '✨','custom_emoji_id':x.get('custom_emoji_id')} for x in stickers.get('stickers',[])]}
        raise HTTPException(status_code=400,detail='Chức năng Admin không hợp lệ.')
    except HTTPException: raise
    except Exception as exc:
        log.exception('Mini App admin action failed: %s',action)
        raise HTTPException(status_code=400,detail=str(exc)[:400])


def main():
    global telegram_application
    if not BOT_TOKEN: raise SystemExit('Thiếu BOT_TOKEN trong .env')
    if not DATABASE_URL: raise SystemExit('Thiếu DATABASE_URL trong .env')
    if not ADMIN_IDS: raise SystemExit('Thiếu ADMIN_IDS trong .env')
    try:
        init_db()
        log.info('Database schema initialized')
    except Exception:
        log.exception('Database initialization failed')
        raise SystemExit('Database initialization failed; check Paella Logs')
    app = Application.builder().token(BOT_TOKEN).build()
    telegram_application = app
    app.add_handler(CommandHandler('start', start)); app.add_handler(CommandHandler('admin', admin_cmd))
    app.add_handler(CallbackQueryHandler(callback)); app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)
    port = int(os.getenv('PORT', '3000'))
    web_thread = threading.Thread(target=lambda: uvicorn.run(web_app, host='0.0.0.0', port=port, log_level='info'), daemon=True)
    web_thread.start()
    log.info('Shop bot + Mini App API started on port %s', port)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__': main()

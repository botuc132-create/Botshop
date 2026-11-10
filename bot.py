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
from fastapi.responses import FileResponse
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
QR_BANK = os.getenv('QR_BANK', 'VietinBank').strip()
QR_ACCOUNT = os.getenv('QR_ACCOUNT', '102886882160').strip()
QR_HOLDER = os.getenv('QR_HOLDER', 'BACH CONG HIEU HUY').strip()
QR_TEMPLATE = os.getenv('QR_TEMPLATE', 'compact').strip()
QR_SHOWINFO = os.getenv('QR_SHOWINFO', 'false').strip().lower()
MINI_APP_URL = os.getenv('MINI_APP_URL', '').strip()
QR_MEMO_PREFIX = 'SEVQR'
QR_MEMO_SUFFIX = os.getenv('QR_MEMO_SUFFIX', 'NAP').strip().upper() or 'NAP'
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
            amount BIGINT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', note TEXT, created_at TEXT NOT NULL,
            expires_at TIMESTAMPTZ)""",
        "ALTER TABLE deposits ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ",
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

async def broadcast(bot, text, photo=None, status_message=None):
    with db() as con: 
        users = con.execute('SELECT user_id FROM users WHERE is_locked=FALSE').fetchall()
    
    total = len(users)
    if total == 0:
        if status_message:
            try:
                await status_message.edit_text('⚠️ Không có người dùng nào để gửi thông báo.', reply_markup=ADMIN_KB)
            except Exception:
                await bot.send_message(chat_id=status_message.chat_id, text='⚠️ Không có người dùng nào để gửi thông báo.')
        return

    success = 0
    fail = 0
    for idx, u in enumerate(users, 1):
        try:
            if photo:
                await bot.send_photo(chat_id=u['user_id'], photo=photo, caption=text, parse_mode='HTML')
            else:
                await bot.send_message(chat_id=u['user_id'], text=text, parse_mode='HTML')
            success += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)

    if status_message:
        try:
            await status_message.edit_text(f'📢 Đã gửi thông báo:\n- Thành công: {success}\n- Thất bại: {fail}', reply_markup=ADMIN_KB)
        except Exception:
            pass

# Khởi tạo ứng dụng FastAPI và chạy ở PORT 8080
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def read_root():
    return {"status": "running", "bot": "Jinny Cheat Bot"}

def run_web():
    uvicorn.run(app, host="0.0.0.0", port=8080)

if __name__ == "__main__":
    init_db()
    # Chạy Web Server FastAPI song song ở luồng riêng
    threading.Thread(target=run_web, daemon=True).start()
    
    # Khởi chạy Telegram Bot
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_cmd))
    
    # Thêm các Handler phụ trợ ở đây nếu có...
    
    log.info("Bot đang khởi động chạy bằng Polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

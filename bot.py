import os
import re
import io
import asyncio
import logging
import json
import hmac
import hashlib
import time
import threading
import secrets
from urllib.parse import parse_qsl
import urllib.request
import requests
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

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove, MessageEntity, WebAppInfo, MenuButtonWebApp, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

DATABASE_URL = os.getenv('DATABASE_URL', '').strip()
BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()
SHOPYURI_API_TOKEN = ''.join(os.getenv('SHOPYURI_API_TOKEN', '').split())
SHOPYURI_API_URL = os.getenv('SHOPYURI_API_URL', 'https://shopyuri.site/api/seller.php').strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip().isdigit()}
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
# Fallback VietQR settings mirror env.example so deposits work even when
# optional QR variables have not been added to Railway Variables.
# Railway environment values always take precedence over these defaults.
QR_BANK = os.getenv('QR_BANK', 'VietinBank').strip() or 'VietinBank'
QR_ACCOUNT = os.getenv('QR_ACCOUNT', '102886882160').strip() or '102886882160'
QR_HOLDER = os.getenv('QR_HOLDER', 'BACH CONG HIEU HUY').strip() or 'BACH CONG HIEU HUY'
QR_TEMPLATE = os.getenv('QR_TEMPLATE', 'compact').strip()
QR_SHOWINFO = os.getenv('QR_SHOWINFO', 'false').strip().lower()
QR_MEMO_PREFIX = 'SEVQR'
QR_MEMO_SUFFIX = os.getenv('QR_MEMO_SUFFIX', 'NAP').strip().upper() or 'NAP'
SEPAY_WEBHOOK_API_KEY = os.getenv('SEPAY_WEBHOOK_API_KEY', '').strip()
SEPAY_AUTO_APPROVE_DELAY = max(0, min(60, int(os.getenv('SEPAY_AUTO_APPROVE_DELAY', '6') or '6')))
MINI_APP_URL = os.getenv('MINI_APP_URL', '').strip()
CUSTOM_BUTTON_EMOJI_ID = '6168207816042487487'
DB_POOL = None
logging.basicConfig(format='%(asctime)s | %(levelname)s | %(name)s | %(message)s', level=LOG_LEVEL)
log = logging.getLogger('key_shop_bot')

web_app = FastAPI(title='Jinny Cheat Mini App API')
telegram_application = None


@web_app.middleware('http')
async def disable_api_caching(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'private, no-store, no-cache, max-age=0, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


# Keep python-telegram-bot 21.6; api_kwargs passes newer Telegram Bot API fields.
def quick_button(text, style='success', **kwargs):
    return InlineKeyboardButton(
        text,
        api_kwargs={
            'style': style,
            'icon_custom_emoji_id': CUSTOM_BUTTON_EMOJI_ID,
        },
        **kwargs,
    )


USER_KB = ReplyKeyboardRemove()

async def configure_miniapp_menu_button(application):
    if not MINI_APP_URL.startswith('https://'):
        log.warning('Mini App menu button skipped: MINI_APP_URL must be HTTPS')
        return
    try:
        await application.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text='Mua Key',
                web_app=WebAppInfo(url=MINI_APP_URL),
            )
        )
        log.info('Default Telegram Mini App menu button configured')
    except Exception:
        log.exception('Could not configure Telegram Mini App menu button')

ADMIN_KB = InlineKeyboardMarkup([
    [quick_button('Thông báo', callback_data='adm:broadcast')],
    [quick_button('Thống kê hệ thống', callback_data='adm:stats')],
    [quick_button('Check LS', callback_data='adm:checkls'), quick_button('Top người SD nhiều nhất', callback_data='adm:topusers')],
    [quick_button('Thông tin người dùng', callback_data='adm:usercheck')],
    [quick_button('Thêm sản phẩm', callback_data='adm:addproduct')],
    [quick_button('Thêm link cách cài', callback_data='adm:addguide')],
    [quick_button('Gán ID Shopyuri', callback_data='adm:mapshop')],
    [quick_button('Sửa giá / thời hạn', callback_data='adm:editproduct')],
    [quick_button('Sửa giá Sell', callback_data='adm:editsellprice')],
    [quick_button('Chi tiết sản phẩm', callback_data='adm:detailproduct')],
    [quick_button('Đổi tên danh mục ngoài', callback_data='adm:renamecat')],
    [quick_button('Dừng bán / Bật bán lại', callback_data='adm:delproduct')],
    [quick_button('Thêm key sản phẩm', callback_data='adm:addkeys')],
    [quick_button('Kiểm tra kho', callback_data='adm:stock')],
    [quick_button('Bảo trì: Bật / Tắt', callback_data='adm:maintenance')],
    [quick_button('Kho Emoji NewsEmoji', callback_data='adm:emoji')],
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

def now(): return datetime.now(ZoneInfo('Asia/Ho_Chi_Minh')).isoformat(timespec='seconds')
def now_vn(): return datetime.now(ZoneInfo('Asia/Ho_Chi_Minh')).strftime('%d/%m/%Y %H:%M:%S')
def format_vn_time(ts):
    if not ts:
        return 'Không rõ'
    try:
        if isinstance(ts, str):
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        else:
            dt = ts
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo('Asia/Ho_Chi_Minh'))
        else:
            dt = dt.astimezone(ZoneInfo('Asia/Ho_Chi_Minh'))
        return dt.strftime('%d/%m/%Y %H:%M:%S')
    except Exception:
        return str(ts)
def make_qr_url(amount, memo):
    if not QR_ACCOUNT:
        raise RuntimeError('QR_ACCOUNT chưa được cấu hình.')
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

def init_db():
    global DB_POOL
    if DB_POOL is None:
        DB_POOL = ConnectionPool(
            conninfo=DATABASE_URL, min_size=1, max_size=20, timeout=15,
            kwargs={'row_factory': dict_row}, open=True
        )
        DB_POOL.wait(timeout=15)
    statements = [
        '''CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY, username TEXT, full_name TEXT NOT NULL DEFAULT '',
            balance BIGINT NOT NULL DEFAULT 0, is_locked BOOLEAN NOT NULL DEFAULT FALSE,
            joined_at TEXT NOT NULL, last_seen TEXT NOT NULL)''',
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_deposit_request_at TIMESTAMPTZ",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_seller BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE product_options ADD COLUMN IF NOT EXISTS shophenry_product_id TEXT",  # legacy column retained for safe DB migration
        "ALTER TABLE product_options ADD COLUMN IF NOT EXISTS shopyuri_product_id TEXT",
        "ALTER TABLE product_options ADD COLUMN IF NOT EXISTS shopyuri_stock BIGINT",
        "ALTER TABLE product_options ADD COLUMN IF NOT EXISTS sell_price BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE purchases ADD COLUMN IF NOT EXISTS supplier_order_code TEXT",
        "ALTER TABLE categories ADD COLUMN IF NOT EXISTS guide_url TEXT",
        "ALTER TABLE categories ADD COLUMN IF NOT EXISTS icon TEXT",
        "CREATE TABLE IF NOT EXISTS bot_settings (setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL, updated_at TEXT NOT NULL)",
        "INSERT INTO bot_settings(setting_key,setting_value,updated_at) VALUES('maintenance','off',NOW()::text) ON CONFLICT(setting_key) DO NOTHING",
        '''CREATE TABLE IF NOT EXISTS categories (
            id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_by BIGINT, created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY, category_id BIGINT REFERENCES categories(id) ON DELETE CASCADE,
            name TEXT NOT NULL, created_by BIGINT, created_at TEXT NOT NULL)''',
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE",
        '''DO $$ BEGIN
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
        END $$''',
        '''CREATE TABLE IF NOT EXISTS product_keys (
            id BIGSERIAL PRIMARY KEY, option_id BIGINT NOT NULL REFERENCES product_options(id) ON DELETE CASCADE,
            key_value TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'available' CHECK(status IN ('available','sold')),
            added_by BIGINT, added_at TEXT NOT NULL, sold_to BIGINT, sold_at TEXT)''',
        '''CREATE TABLE IF NOT EXISTS purchases (
            id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(user_id),
            category_name TEXT NOT NULL, product_name TEXT NOT NULL, duration_label TEXT NOT NULL,
            quantity INTEGER NOT NULL, unit_price BIGINT NOT NULL, total_price BIGINT NOT NULL,
            keys_text TEXT NOT NULL, created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS deposits (
            id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(user_id),
            amount BIGINT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', note TEXT, created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS balance_transactions (
            id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(user_id),
            admin_id BIGINT NOT NULL, action TEXT NOT NULL CHECK(action IN ('add','subtract','deposit')) ,
            amount BIGINT NOT NULL CHECK(amount > 0), balance_after BIGINT NOT NULL,
            note TEXT, created_at TEXT NOT NULL)''',
        '''DO $$ DECLARE r RECORD; BEGIN
            FOR r IN
                SELECT conname FROM pg_constraint
                WHERE conrelid = 'public.product_options'::regclass
                  AND contype = 'c'
                  AND pg_get_constraintdef(oid) ILIKE '%duration%'
            LOOP
                EXECUTE format('ALTER TABLE public.product_options DROP CONSTRAINT %I', r.conname);
            END LOOP;
        END $$''',
        "CREATE INDEX IF NOT EXISTS idx_product_keys_stock ON product_keys(option_id,status)",
        "CREATE INDEX IF NOT EXISTS idx_purchases_user ON purchases(user_id,created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_users_username ON users(LOWER(username))",
        "CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen DESC)",
        "CREATE INDEX IF NOT EXISTS idx_deposits_user ON deposits(user_id,id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_deposits_status ON deposits(status,id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_balance_transactions_user ON balance_transactions(user_id,id DESC)",
        "ALTER TABLE deposits ADD COLUMN IF NOT EXISTS sepay_transaction_id TEXT",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_deposits_sepay_transaction ON deposits(sepay_transaction_id) WHERE sepay_transaction_id IS NOT NULL",
        "CREATE TABLE IF NOT EXISTS sepay_webhook_events (transaction_id TEXT PRIMARY KEY, deposit_id BIGINT, received_at TEXT NOT NULL, payload JSONB)",
        "CREATE TABLE IF NOT EXISTS discount_coupons (id BIGSERIAL PRIMARY KEY, code CHAR(5) UNIQUE NOT NULL, discount_percent INTEGER NOT NULL CHECK(discount_percent BETWEEN 1 AND 90), max_uses INTEGER NOT NULL CHECK(max_uses > 0), used_count INTEGER NOT NULL DEFAULT 0, is_active BOOLEAN NOT NULL DEFAULT TRUE, created_by BIGINT, created_at TEXT NOT NULL)",
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
        rows.append([quick_button('⚡ Mở Mini App – Mua hàng nhanh', web_app=WebAppInfo(url=MINI_APP_URL))])
    rows.extend([
        [quick_button('🛍 Mua hàng – Cửa Hàng', callback_data='shop:home'), quick_button('💳 Nạp Tiền', callback_data='menu:deposit')],
        [quick_button('👤 Tài Khoản', callback_data='menu:account'), quick_button('📜 Lịch Sử Mua', callback_data='menu:history')],
        [quick_button('📖 Hướng dẫn cách cài', callback_data='menu:guide')],
        [quick_button('🆘 Hỗ Trợ', callback_data='menu:support')],
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
    with db() as con: return con.execute('SELECT c.id,c.name,c.guide_url FROM categories c WHERE EXISTS (SELECT 1 FROM products p WHERE p.category_id=c.id AND p.is_active=TRUE) ORDER BY c.id').fetchall()
def get_products(category_id):
    with db() as con: return con.execute('SELECT id,name FROM products WHERE category_id::text=?::text AND is_active=TRUE ORDER BY id', (category_id,)).fetchall()

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
            con.execute('''INSERT INTO users(user_id,username,full_name,joined_at,last_seen) VALUES(?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username,full_name=EXCLUDED.full_name,last_seen=EXCLUDED.last_seen''',
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
        if MINI_APP_URL.startswith('https://'):
            await message.reply_text(
                '👇 Nhấn nút Menu cạnh ô nhập tin nhắn để mở Mini App.',
                reply_markup=USER_KB
            )
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
    rows = [[quick_button(f"🛍 {c['name']}", callback_data=f"shop:cat:{c['id']}")] for c in cats]
    rows.append([InlineKeyboardButton('🌐 Quay Lại', callback_data='shop:home')])
    txt = '🎮 DANH MỤC: SHOP\n\n❝ Chọn danh mục bên dưới để xem sản phẩm và gói thời hạn: ❞'
    if edit: await message.edit_text(txt, reply_markup=InlineKeyboardMarkup(rows))
    else: await message.reply_text(txt, reply_markup=InlineKeyboardMarkup(rows))

async def show_category(q, category_id, user_id):
    with db() as con:
        cat = con.execute('SELECT * FROM categories WHERE id=?', (category_id,)).fetchone()
        prods = con.execute('SELECT id, name FROM products WHERE category_id::text=?::text AND is_active=TRUE', (category_id,)).fetchall()
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
        src_str = f"Kho còn: {int(o.get('shopyuri_stock') or 0)}" if o.get('shopyuri_product_id') else f"Kho còn: {stock_for_option(o['id'])}"
            
        current_price = o['sell_price'] if (is_seller and o['sell_price'] > 0) else o['price']
        btn_text = f"🛍 {o['product_name']} - {o['duration_label']} — {current_price:,}đ [{src_str}]"
        rows.append([quick_button(btn_text, callback_data=f"shop:option:{o['id']}")])
    rows.append([InlineKeyboardButton('🌐 Quay Lại', callback_data='shop:home')])
    
    txt = f"✨ {escape(cat['name'])}\n\n❝ Chọn gói sản phẩm bên dưới để xem chi tiết và mua: ❞"
    await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(rows), parse_mode='HTML')

async def show_quantity(q, option_id, context, user_id):
    with db() as con:
        o=con.execute('SELECT po.*,p.name AS product_name,p.category_id,c.name AS category_name FROM product_options po JOIN products p ON p.id::text=po.product_id::text JOIN categories c ON c.id::text=p.category_id::text WHERE po.id=? AND p.is_active=TRUE',(option_id,)).fetchone()
        u_row = con.execute('SELECT is_seller FROM users WHERE user_id=?',(user_id,)).fetchone()
    if not o: await q.edit_message_text('Gói sản phẩm không còn tồn tại.'); return
    is_seller = bool(u_row and u_row['is_seller'])
    current_price = o['sell_price'] if (is_seller and o['sell_price'] > 0) else o['price']
    context.user_data['selected_option']=option_id
    buttons=[
        [quick_button('🛍 Mua 1 Key',callback_data=f'shop:qty:{option_id}:1'),
         quick_button('🛍 Mua 2 Key',callback_data=f'shop:qty:{option_id}:2')],
        [quick_button('🛍 Mua 3 Key',callback_data=f'shop:qty:{option_id}:3'),
         quick_button('🛍 Mua 5 Key',callback_data=f'shop:qty:{option_id}:5')],
        [quick_button('🛍 Mua 10 Key',callback_data=f'shop:qty:{option_id}:10')],
        [quick_button('🛍 Mua số lượng khác',callback_data=f'shop:qty:{option_id}:other')],
        [InlineKeyboardButton('📂 Quay Lại',callback_data=f"shop:cat:{o['category_id']}")]
    ]
    src_text = f"Kho còn: {int(o.get('shopyuri_stock') or 0)} key" if o.get('shopyuri_product_id') else f"Kho còn: {stock_for_option(option_id)} key"

    await q.edit_message_text(
        f"🛒 {escape(o['product_name'])}\n⏳ {escape(o['duration_label'])}"
        f"\n💵 Giá: {current_price:,}đ / key\n📦 Nguồn: {src_text}"
        f"\n\n🔑 Chọn số lượng Key muốn mua để tiến hành thanh toán:",
        reply_markup=InlineKeyboardMarkup(buttons),parse_mode='HTML')

async def show_confirm(q, option_id, qty, context, user_id):
    with db() as con:
        o=con.execute('SELECT po.*,p.name AS product_name,p.category_id,c.name AS category_name FROM product_options po JOIN products p ON p.id::text=po.product_id::text JOIN categories c ON c.id::text=p.category_id::text WHERE po.id=? AND p.is_active=TRUE',(option_id,)).fetchone()
        u_row = con.execute('SELECT is_seller FROM users WHERE user_id=?',(user_id,)).fetchone()
    if not o: await q.edit_message_text('Sản phẩm không còn tồn tại.'); return
    is_seller = bool(u_row and u_row['is_seller'])
    current_price = o['sell_price'] if (is_seller and o['sell_price'] > 0) else o['price']
    total=current_price*qty
    is_shopyuri=bool(o.get('shopyuri_product_id'))
    context.user_data.update(pending_purchase={'option_id':option_id,'quantity':qty,'total':total})
    payment_note = ('💳 Thanh toán: Trừ số dư tài khoản trong bot; Shopyuri cấp key.'
                    if is_shopyuri else '💳 Thanh toán: Trừ số dư tài khoản trong bot.')
    await q.edit_message_text(
        f"🧾 XÁC NHẬN MUA HÀNG\n\n📁 Danh mục: {escape(o['category_name'])}"
        f"\n📦 Sản phẩm: {escape(o['product_name'])}\n⏳ Thời hạn: {escape(o['duration_label'])}"
        f"\n🔢 Số lượng: {qty}\n💰 Giá bán: {total:,}đ\n{payment_note}\n\nBạn xác nhận mua chứ?",
        reply_markup=InlineKeyboardMarkup([
            [quick_button('🛍 XÁC NHẬN MUA HÀNG', callback_data='shop:confirm')],
            [quick_button('HỦY', style='danger', callback_data='shop:cancel')],
        ]),
        parse_mode='HTML',
    )

def shopyuri_buy(product_id, quantity):
    """Mua key qua Shopyuri; token chỉ được đọc từ biến môi trường Railway."""
    if not SHOPYURI_API_TOKEN:
        raise RuntimeError('Thiếu SHOPYURI_API_TOKEN trong Railway Variables.')

    try:
        product_id = int(product_id)
        quantity = int(quantity)
    except (TypeError, ValueError):
        raise RuntimeError('Shopyuri product_id hoặc quantity không hợp lệ.')

    if product_id <= 0 or quantity < 1 or quantity > 100:
        raise RuntimeError('Shopyuri product_id hoặc quantity nằm ngoài giới hạn.')

    url = SHOPYURI_API_URL.split('?', 1)[0] + '?action=buy'
    headers = {
        'Authorization': f'Bearer {SHOPYURI_API_TOKEN}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'User-Agent': 'HmeShopee/1.0',
    }
    payload = {'product_id': product_id, 'quantity': quantity}

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=35)
    except requests.RequestException as exc:
        raise RuntimeError(f'Không kết nối được Shopyuri: {exc}')

    try:
        result = response.json()
    except ValueError:
        result = {}

    if response.status_code >= 400:
        # Keep the upstream reason visible; HTTP 403 can be caused by token permissions,
        # IP restrictions, or an invalid supplier product mapping and cannot be bypassed locally.
        detail = ''
        if isinstance(result, dict):
            detail = str(result.get('message') or result.get('error') or result.get('detail') or '').strip()
        if not detail:
            detail = (response.text or '').strip()[:700]
        if not detail:
            detail = 'Nhà cung cấp không trả nội dung giải thích.'
        raise RuntimeError(f'Shopyuri trả HTTP {response.status_code}: {detail}')

    if not isinstance(result, dict):
        raise RuntimeError('Shopyuri trả về dữ liệu JSON không hợp lệ.')

    success = (
        result.get('success') is True
        or str(result.get('status', '')).lower() in ('success', 'ok', 'completed')
    )
    if not success:
        detail = result.get('message') or result.get('error') or result.get('detail')
        raise RuntimeError(str(detail or 'Shopyuri không xác nhận đơn hàng.'))

    data = result.get('data') if isinstance(result.get('data'), dict) else result
    raw_keys = (
        data.get('keys') or data.get('key') or data.get('key_list')
        or data.get('license_keys') or result.get('keys') or result.get('key')
    )
    if isinstance(raw_keys, list):
        keys = [str(item).strip() for item in raw_keys if str(item).strip()]
    elif isinstance(raw_keys, str):
        keys = [line.strip() for line in raw_keys.replace('\\r', '').split('\\n') if line.strip()]
        if len(keys) == 1 and quantity > 1 and ' | ' in keys[0]:
            keys = [item.strip() for item in keys[0].split(' | ') if item.strip()]
    else:
        keys = []

    order_code = str(
        data.get('order_code') or data.get('order_id')
        or data.get('transaction_id') or ''
    )
    if len(keys) != quantity:
        raise RuntimeError(
            'Shopyuri báo thành công nhưng số key trả về không khớp số lượng. '
            + (f'Mã đơn: {order_code}. ' if order_code else '')
            + 'Bot chưa trừ số dư; Admin cần kiểm tra đơn trên nhà cung cấp trước khi mua lại.'
        )
    return '\\n'.join(keys), order_code

async def do_purchase(q, update, context):
    if not q.message or q.message.chat.type != 'private':
        context.user_data.pop('pending_purchase', None)
        await q.edit_message_text(
            '🔒 Để tránh lộ key, bot chỉ xử lý mua hàng trong chat riêng. '
            'Hãy mở chat riêng với bot để tiếp tục.'
        )
        return
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
    is_shopyuri = False
    try:
        con.execute('BEGIN')
        user = con.execute('SELECT balance,is_locked,is_seller FROM users WHERE user_id=? FOR UPDATE', (uid,)).fetchone()
        opt = con.execute('''SELECT po.*,p.name AS product_name,p.category_id,
            c.name AS category_name FROM product_options po
            JOIN products p ON p.id::text=po.product_id::text
            JOIN categories c ON c.id::text=p.category_id::text WHERE po.id=? AND p.is_active=TRUE''', (oid,)).fetchone()
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
        is_shopyuri = bool(opt.get('shopyuri_product_id'))

        if user['balance'] < total:
            con.rollback()
            await q.edit_message_text(
                '⚠️ Hệ thống đang gặp sự cố xử lý đơn hàng. Vui lòng chờ một chút hoặc nhắn tin Admin để được hỗ trợ.'
            )
            return

        if is_shopyuri:
            try:
                key_text, supplier_order = await asyncio.to_thread(
                    shopyuri_buy, opt['shopyuri_product_id'], qty
                )
                keys = []
            except Exception as api_error:
                con.rollback()
                log.warning('Shopyuri purchase failed for user=%s product=%s: %s',
                            uid, opt['shopyuri_product_id'], api_error)
                await q.edit_message_text('⚠️ Hệ thống đang gặp sự cố xử lý đơn hàng. Vui lòng chờ một chút hoặc nhắn tin Admin để được hỗ trợ. Không tự mua lại nếu đơn trước chưa được Admin kiểm tra.')
                return
        else:
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
        con.execute('UPDATE users SET balance=balance-? WHERE user_id=?', (total, uid))
        if not is_shopyuri:
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
        log.exception('Purchase record failed (Shopyuri order=%s)', supplier_order or 'n/a')
        if is_shopyuri and key_text:
            await q.edit_message_text(
                '⚠️ Shopyuri đã tạo đơn thành công nhưng bot không lưu được lịch sử. '
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

    with db() as guide_con:
        guide_cat = guide_con.execute('SELECT guide_url FROM categories WHERE id=?', (opt['category_id'],)).fetchone()
    guide_url = guide_cat['guide_url'] if guide_cat else None
    success_msg = (
        "🎉 <b>MUA HÀNG THÀNH CÔNG!</b> 🎉\n\n"
        f"🎮 Game: <b>{escape(opt['category_name'])}</b>\n"
        f"📦 Sản phẩm: <b>{escape(opt['product_name'])} - {escape(opt['duration_label'])}</b>\n"
        f"🔢 Số lượng: <b>{qty}</b>\n"
        f"💰 Tổng tiền: <b>{total:,}đ</b>\n\n"
        f"🔑 <b>KEY CỦA BẠN:</b>\n<code>{escape(key_text)}</code>"
    )
    buttons = [[quick_button('🛍 Mua hàng tiếp', callback_data='shop:home')]]
    if guide_url:
        buttons.append([InlineKeyboardButton('📖 Cách cài', url=guide_url)])
    await q.edit_message_text(success_msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(buttons))

async def user_history(q, uid):
    if not q.message or q.message.chat.type != 'private':
        await q.edit_message_text(
            '🔒 Lịch sử có chứa key chỉ hiển thị trong chat riêng với bot. '
            'Hãy mở bot trực tiếp để xem.'
        )
        return
    with db() as con: rows = con.execute('SELECT * FROM purchases WHERE user_id=? ORDER BY id DESC LIMIT 20', (uid,)).fetchall()
    if not rows:
        await q.edit_message_text('📜 Bạn chưa có lịch sử mua hàng.', reply_markup=inline_back('menu:home'))
        return
    out = ['📜 LỊCH SỬ MUA HÀNG']
    for r in rows:
        out.append(
            f"\n🧾 <b>Đơn #{r['id']}</b> · {escape(str(r['created_at']))}\n"
            f"📦 {escape(r['product_name'])} · {escape(r['duration_label'])}\n"
            f"🔢 Số lượng: {r['quantity']} · 💰 {r['total_price']:,}đ\n"
            f"🔑 KEY:\n<pre>{escape(r['keys_text'] or 'Không có key')}</pre>"
        )
    await q.edit_message_text('\n'.join(out), parse_mode='HTML', reply_markup=inline_back('menu:home'))

async def broadcast(bot, payload_data, status_message=None, reply_markup=None):
    if isinstance(payload_data, str):
        payload_data = {'kind': 'text', 'text': payload_data}

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

    kind = payload_data.get('kind') or ('photo' if payload_data.get('photo') else 'text')
    text = payload_data.get('text') or ''
    photo = payload_data.get('photo')
    sticker = payload_data.get('sticker')
    entities = payload_data.get('entities')
    from_chat_id = payload_data.get('from_chat_id')
    message_id = payload_data.get('message_id')

    sent = 0
    for index, u in enumerate(users, 1):
        uid = u['user_id']
        try:
            if kind == 'forward' and from_chat_id and message_id:
                await bot.forward_message(
                    chat_id=uid,
                    from_chat_id=from_chat_id,
                    message_id=message_id,
                )
            elif kind == 'sticker' and sticker:
                await bot.send_sticker(
                    chat_id=uid,
                    sticker=sticker,
                    reply_markup=reply_markup,
                )
            elif kind == 'copy' and from_chat_id and message_id:
                await bot.copy_message(
                    chat_id=uid,
                    from_chat_id=from_chat_id,
                    message_id=message_id,
                    reply_markup=reply_markup,
                )
            elif photo:
                await bot.send_photo(
                    chat_id=uid,
                    photo=photo,
                    caption=text or None,
                    caption_entities=entities,
                    reply_markup=reply_markup,
                )
            else:
                await bot.send_message(
                    chat_id=uid,
                    text=text,
                    entities=entities,
                    reply_markup=reply_markup,
                )
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

        if not pending_bc:
            msg_obj = q.message
            if msg_obj:
                raw_text = msg_obj.caption or msg_obj.text or ''
                if 'XEM TRƯỚC THÔNG BÁO:\n\n' in raw_text:
                    bc_text = raw_text.split('XEM TRƯỚC THÔNG BÁO:\n\n', 1)[-1]
                else:
                    bc_text = raw_text
                photo = msg_obj.photo[-1].file_id if msg_obj.photo else None
                pending_bc = {'kind': 'photo' if photo else 'text', 'text': bc_text, 'photo': photo}

        if not pending_bc:
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

        await broadcast(context.bot, pending_bc, status_message=status_msg)
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
            total_deposit = con.execute("SELECT COALESCE(SUM(amount),0) AS total FROM deposits WHERE status='approved'").fetchone()['total'] or 0
            total_balance = con.execute("SELECT COALESCE(SUM(balance),0) AS total FROM users").fetchone()['total'] or 0
            vn_now = datetime.now(ZoneInfo('Asia/Ho_Chi_Minh'))
            today_start = vn_now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec='seconds')
            tomorrow_start = (vn_now.replace(hour=0, minute=0, second=0, microsecond=0) + __import__('datetime').timedelta(days=1)).isoformat(timespec='seconds')
            today_deposit = con.execute("SELECT COALESCE(SUM(amount),0) AS total FROM deposits WHERE status='approved' AND created_at>=? AND created_at<?", (today_start, tomorrow_start)).fetchone()['total'] or 0
        txt = (
            f"📊 THỐNG KÊ HỆ THỐNG\n\n"
            f"👥 Tổng số người dùng: {user_count:,}\n"
            f"💰 Tổng tiền đã nạp thành công: {total_deposit:,}đ\n"
            f"📅 Tổng nạp hôm nay: {today_deposit:,}đ\n"
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
        await q.edit_message_text(
            '🆘 <b>HỖ TRỢ</b>\nNếu cần hỗ trợ, hãy nhấn nút bên dưới để liên hệ.',
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('💬 Liên Hệ', url='https://t.me/huybuwin')],
                [InlineKeyboardButton('↩️ Quay lại', callback_data='menu:home')]
            ])
        )
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
            await q.message.reply_text(
                'Gửi nội dung thông báo bất kỳ (văn bản có định dạng, ảnh, video, GIF, nhãn dán/sticker hoặc chuyển tiếp tin nhắn từ người/nhóm/kênh khác):'
            )
    elif data == 'adm:checkls':
        kb = InlineKeyboardMarkup([
            [quick_button('LS Mua Key', callback_data='adm:ls_buy'),
             quick_button('LS Nạp', callback_data='adm:ls_deposit')],
            [InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]
        ])
        await q.edit_message_text('📜 <b>KIỂM TRA LỊCH SỬ HỆ THỐNG</b>\n\nChọn loại lịch sử muốn xem:', reply_markup=kb, parse_mode='HTML')
    elif data == 'adm:ls_buy':
        with db() as con:
            rows = con.execute('''
                SELECT p.id, p.user_id, u.username, p.product_name, p.duration_label, p.keys_text, p.created_at
                FROM purchases p
                LEFT JOIN users u ON u.user_id = p.user_id
                ORDER BY p.id DESC LIMIT 15
            ''').fetchall()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('📥 Xem LS Nạp', callback_data='adm:ls_deposit'),
             InlineKeyboardButton('↩️ Quay lại', callback_data='adm:checkls')]
        ])
        if not rows:
            await q.edit_message_text('📜 <b>LỊCH SỬ MUA KEY GẦN NHẤT</b>\n\nChưa có giao dịch mua key nào.', reply_markup=kb, parse_mode='HTML')
            return
        lines = ['📜 <b>15 LỊCH SỬ MUA KEY GẦN NHẤT</b>\n']
        for idx, r in enumerate(rows, 1):
            uname = f"@{escape(r['username'])}" if r.get('username') else 'Không có @'
            vn_time = format_vn_time(r.get('created_at'))
            raw_keys = (r.get('keys_text') or '').strip()
            key_display = escape(raw_keys.replace('\n', ' | '))
            if len(key_display) > 120:
                key_display = key_display[:117] + '...'
            lines.append(
                f"<b>{idx}.</b> {uname} | ID: <code>{r['user_id']}</code>\n"
                f"   🔑 Key: <code>{key_display or 'Trống'}</code>\n"
                f"   🕒 Giờ VN: {vn_time}"
            )
        msg_text = '\n'.join(lines)
        if len(msg_text) > 4000:
            msg_text = msg_text[:3990] + '\n...'
        await q.edit_message_text(msg_text, reply_markup=kb, parse_mode='HTML')
    elif data == 'adm:ls_deposit':
        with db() as con:
            rows = con.execute('''
                SELECT d.id, d.user_id, u.username, d.amount, d.created_at
                FROM deposits d
                LEFT JOIN users u ON u.user_id = d.user_id
                WHERE d.status = 'approved'
                ORDER BY d.id DESC LIMIT 15
            ''').fetchall()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('🔑 Xem LS Mua Key', callback_data='adm:ls_buy'),
             InlineKeyboardButton('↩️ Quay lại', callback_data='adm:checkls')]
        ])
        if not rows:
            await q.edit_message_text('📥 <b>LỊCH SỬ NẠP TIỀN GẦN NHẤT</b>\n\nChưa có giao dịch nạp tiền thành công nào.', reply_markup=kb, parse_mode='HTML')
            return
        lines = ['📥 <b>15 LỊCH SỬ NẠP TIỀN GẦN NHẤT</b>\n']
        for idx, r in enumerate(rows, 1):
            uname = f"@{escape(r['username'])}" if r.get('username') else 'Không có @'
            vn_time = format_vn_time(r.get('created_at'))
            lines.append(
                f"<b>{idx}.</b> {uname} | ID: <code>{r['user_id']}</code>\n"
                f"   💰 Số tiền: <b>{int(r['amount']):,}đ</b>\n"
                f"   🕒 Giờ VN: {vn_time}"
            )
        msg_text = '\n'.join(lines)
        if len(msg_text) > 4000:
            msg_text = msg_text[:3990] + '\n...'
        await q.edit_message_text(msg_text, reply_markup=kb, parse_mode='HTML')
    elif data == 'adm:topusers':
        with db() as con:
            rows = con.execute('''
                SELECT u.user_id, u.username, u.full_name, u.balance,
                       COALESCE(p.total_spent, 0) AS total_spent,
                       COALESCE(p.order_count, 0) AS order_count,
                       COALESCE(d.total_dep, 0) AS total_dep
                FROM users u
                LEFT JOIN (
                    SELECT user_id, SUM(total_price) AS total_spent, COUNT(*) AS order_count
                    FROM purchases
                    GROUP BY user_id
                ) p ON p.user_id = u.user_id
                LEFT JOIN (
                    SELECT user_id, SUM(amount) AS total_dep
                    FROM deposits
                    WHERE status = 'approved'
                    GROUP BY user_id
                ) d ON d.user_id = u.user_id
                ORDER BY COALESCE(p.total_spent, 0) DESC, COALESCE(d.total_dep, 0) DESC, u.balance DESC
                LIMIT 10
            ''').fetchall()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]])
        if not rows:
            await q.edit_message_text('🏆 <b>TOP 10 NGƯỜI SỬ DỤNG NHIỀU NHẤT</b>\n\nChưa có dữ liệu người dùng.', reply_markup=kb, parse_mode='HTML')
            return
        medals = ['🥇', '🥈', '🥉']
        lines = ['🏆 <b>TOP 10 NGƯỜI SỬ DỤNG NHIỀU NHẤT</b>\n']
        for idx, r in enumerate(rows, 1):
            rank = medals[idx - 1] if idx <= 3 else f"<b>#{idx}</b>"
            uname = f"@{escape(r['username'])}" if r.get('username') else escape(r.get('full_name') or 'Không có @')
            lines.append(
                f"{rank} {uname} | ID: <code>{r['user_id']}</code>\n"
                f"   🛒 Đã tiêu: <b>{int(r['total_spent']):,}đ</b> ({int(r['order_count'])} đơn) | 💳 Nạp: {int(r['total_dep']):,}đ"
            )
        await q.edit_message_text('\n'.join(lines), reply_markup=kb, parse_mode='HTML')
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
        await q.edit_message_text('Chọn danh mục chứa sản phẩm cần gán ID Shopyuri:', reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(c['name'], callback_data='adm:mapshopcat:{}'.format(c['id']))] for c in cats] + [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data.startswith('adm:mapshopcat:'):
        cid = int(data.split(':')[2])
        ps = get_products(cid)
        await q.edit_message_text('Chọn sản phẩm cần gán ID Shopyuri:', reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(p['name'], callback_data='adm:mapshopprod:{}'.format(p['id']))] for p in ps] + [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:mapshop')]]))
    elif data.startswith('adm:mapshopprod:'):
        pid = int(data.split(':')[2])
        with db() as con: options = con.execute('SELECT id,duration_label,shopyuri_product_id FROM product_options WHERE product_id=? ORDER BY id', (pid,)).fetchall()
        await q.edit_message_text('Chọn gói thời hạn cần gán ID Shopyuri:', reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"{o['duration_label']} · ID: {o['shopyuri_product_id'] or 'Chưa gán'}", callback_data=f"adm:mapshopopt:{o['id']}")] for o in options] + [[InlineKeyboardButton('↩️ Quay lại', callback_data=f'adm:mapshopcat:{get_product_category_id(pid)}')]]))
    elif data.startswith('adm:mapshopopt:'):
        oid = int(data.split(':')[2])
        context.user_data.update(flow='map_shopyuri_id', shopyuri_target_option=oid)
        if q.message:
            await q.message.reply_text('Nhập product_id của đúng gói thời hạn này trên Shopyuri (chỉ nhập ID dạng số):')
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
        rows += [[InlineKeyboardButton('➕ Thêm thời hạn', callback_data=f'adm:addduration:{pid}')],
                 [InlineKeyboardButton('🗑 Xóa thời hạn', callback_data=f'adm:delduration:{pid}')],
                 [InlineKeyboardButton('↩️ Quay lại', callback_data='adm:detailproduct')]]
        await q.edit_message_text('📋 CHI TIẾT SẢN PHẨM\nChọn gói để sửa giá hoặc quản lý thời hạn:', reply_markup=InlineKeyboardMarkup(rows))
    elif data.startswith('adm:addduration:'):
        pid = int(data.split(':')[2])
        context.user_data.update(flow='add_product_duration', duration_product_id=pid)
        if q.message:
            await q.message.reply_text('Nhập thời hạn muốn thêm (ví dụ: 12 giờ, 2 ngày, 90 ngày):')
    elif data.startswith('adm:delduration:'):
        pid = int(data.split(':')[2])
        with db() as con:
            opts = con.execute('SELECT id,duration_label FROM product_options WHERE product_id=? ORDER BY id', (pid,)).fetchall()
        rows = [[InlineKeyboardButton(f"{o['duration_label']}", callback_data=f"adm:deldurationconfirm:{o['id']}:{pid}")] for o in opts]
        rows.append([InlineKeyboardButton('↩️ Quay lại', callback_data=f'adm:detailprod:{pid}')])
        await q.edit_message_text('Chọn thời hạn muốn xóa:', reply_markup=InlineKeyboardMarkup(rows))
    elif data.startswith('adm:deldurationconfirm:'):
        parts = data.split(':'); oid, pid = int(parts[2]), int(parts[3])
        with db() as con:
            row = con.execute('SELECT duration_label FROM product_options WHERE id=? AND product_id=?', (oid,pid)).fetchone()
        if not row:
            await q.answer('Thời hạn không còn tồn tại.', show_alert=True); return
        context.user_data['delete_duration_option_id'] = oid
        await q.edit_message_text(f"Xác nhận xóa thời hạn {row['duration_label']}? Toàn bộ key gắn với gói này sẽ bị xóa khỏi kho; lịch sử đơn mua vẫn được giữ.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton('⚠️ XÁC NHẬN XÓA', callback_data='adm:deldurationyes')],
            [InlineKeyboardButton('Hủy', callback_data=f'adm:detailprod:{pid}')]]))
    elif data == 'adm:deldurationyes':
        oid = context.user_data.pop('delete_duration_option_id', None)
        if not oid:
            await q.answer('Không tìm thấy thời hạn cần xóa.', show_alert=True); return
        with db() as con:
            con.execute('DELETE FROM product_options WHERE id=?', (oid,))
        await q.edit_message_text('✅ Đã xóa thời hạn và key gắn với gói đó. Lịch sử đơn mua vẫn được giữ.', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('↩️ Quay lại Admin', callback_data='adm:menu')]]))
    elif data.startswith('adm:detailoption:'):
        oid = int(data.split(':')[2])
        with db() as con:
            opt = con.execute('SELECT po.*,p.id AS product_id,p.name AS product_name FROM product_options po JOIN products p ON p.id=po.product_id WHERE po.id=?', (oid,)).fetchone()
        if not opt:
            await q.answer('Gói sản phẩm không còn tồn tại.', show_alert=True); return
        await q.edit_message_text(
            f"📦 {opt['product_name']}\n⏳ Thời hạn: {opt['duration_label']}\n💰 Giá: {opt['price']:,}đ\n💰 Giá Sell: {opt['sell_price']:,}đ\n\nChọn thao tác:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('📝 Đổi thời hạn', callback_data=f"adm:renameoption:{oid}")],
                [InlineKeyboardButton('✏️ Sửa giá thường', callback_data=f"adm:optionprice:{oid}")],
                [InlineKeyboardButton('↩️ Quay lại', callback_data=f"adm:detailprod:{opt['product_id']}")]
            ]))
    elif data.startswith('adm:renameoption:'):
        oid = int(data.split(':')[2])
        with db() as con:
            opt = con.execute('SELECT po.duration_label,p.id AS product_id,p.name AS product_name FROM product_options po JOIN products p ON p.id=po.product_id WHERE po.id=?', (oid,)).fetchone()
        if not opt:
            await q.answer('Gói sản phẩm không còn tồn tại.', show_alert=True); return
        context.user_data.update(flow='rename_option_duration', rename_option_id=oid, rename_option_product_id=opt['product_id'])
        if q.message:
            await q.message.reply_text(f"Thời hạn hiện tại: {opt['duration_label']}\nNhập thời hạn mới (ví dụ: 1 ngày, 12 giờ, 7 ngày):")
    elif data.startswith('adm:optionprice:'):
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
        await q.edit_message_text('Chọn thao tác quản lý bán sản phẩm:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('📦 Dừng bán / Bật bán lại sản phẩm', callback_data='adm:delinner')], [InlineKeyboardButton('📁 Quản lý danh mục', callback_data='adm:delcat')], [InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data == 'adm:delcat':
        cats = get_categories()
        await q.edit_message_text('Chọn danh mục ngoài cần xóa:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(c['name'], callback_data=f"adm:delcatconfirm:{c['id']}")] for c in cats] + [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data.startswith('adm:delcatconfirm:'):
        cid = int(data.split(':')[2]); context.user_data['delete_target'] = ('category', cid); await q.edit_message_text('Xác nhận xóa danh mục và toàn bộ sản phẩm/key bên trong?', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⚠️ XÁC NHẬN XÓA', callback_data='adm:deleteconfirm')], [InlineKeyboardButton('Hủy', callback_data='adm:menu')]]))
    elif data == 'adm:delinner':
        with db() as con: cats = con.execute('SELECT id,name FROM categories ORDER BY id').fetchall()
        await q.edit_message_text('Chọn danh mục chứa sản phẩm cần dừng/bật bán:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(c['name'], callback_data=f"adm:delinnercat:{c['id']}")] for c in cats] + [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data.startswith('adm:delinnercat:'):
        cid = int(data.split(':')[2]);
        with db() as con: ps = con.execute('SELECT id,name,is_active FROM products WHERE category_id=? ORDER BY id',(cid,)).fetchall()
        await q.edit_message_text('Chọn sản phẩm. Trạng thái hiện tại được ghi rõ:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(('🟢 ĐANG BÁN · ' if p['is_active'] else '⛔ ĐÃ DỪNG · ')+p['name'], callback_data=f"adm:delinnerconfirm:{p['id']}")] for p in ps] + [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data.startswith('adm:delinnerconfirm:'):
        pid = int(data.split(':')[2])
        with db() as con:
            row=con.execute('SELECT name,is_active FROM products WHERE id=?',(pid,)).fetchone()
            if row: con.execute('UPDATE products SET is_active=? WHERE id=?',(not bool(row['is_active']),pid))
        if not row: await q.answer('Không tìm thấy sản phẩm.', show_alert=True); return
        await q.edit_message_text(('🟢 Đã bật bán lại: ' if not row['is_active'] else '⛔ Đã dừng bán: ')+row['name']+'\n\nSản phẩm, key, giá và lịch sử vẫn được giữ nguyên.', reply_markup=ADMIN_KB)
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
        label_fallback = DURATIONS.get(dur, dur)
        with db() as con:
            opt = con.execute('SELECT id,duration_label FROM product_options WHERE product_id::text=?::text AND duration=?', (pid, dur)).fetchone()
            if not opt:
                cur_ins = con.execute('INSERT INTO product_options(product_id, duration, duration_label, price, sell_price) VALUES (?, ?, ?, 0, 0) RETURNING id', (pid, dur, label_fallback))
                opt_id = cur_ins.fetchone()['id']
                con.commit()
            else:
                opt_id = opt['id']
        context.user_data.update(flow='add_product_keys', key_option_id=opt_id, key_duration=dur)
        label = opt['duration_label'] if opt else label_fallback
        if q.message:
            await q.message.reply_text(f'Gửi danh sách key {label}, mỗi key một dòng:')
    elif data == 'adm:stock':
        cats = get_categories()
        await q.edit_message_text('Chọn danh mục để kiểm tra kho:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(c['name'], callback_data=f"adm:stockcat:{c['id']}")] for c in cats] + [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data.startswith('adm:stockcat:'):
        cid = int(data.split(':')[2]); ps = get_products(cid); await q.edit_message_text('Chọn sản phẩm:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(p['name'], callback_data=f"adm:stockprod:{p['id']}")] for p in ps] + [[InlineKeyboardButton('↩️ Quay lại', callback_data='adm:menu')]]))
    elif data.startswith('adm:stockprod:'):
        pid = int(data.split(':')[2])
        with db() as con:
            product = con.execute('SELECT name,category_id FROM products WHERE id=?', (pid,)).fetchone()
            rows = con.execute('''SELECT po.id,po.duration_label,po.price,po.sell_price,
                COUNT(pk.id) FILTER (WHERE pk.status='available') AS available,
                COUNT(pk.id) AS total
                FROM product_options po LEFT JOIN product_keys pk ON pk.option_id=po.id
                WHERE po.product_id=? GROUP BY po.id ORDER BY po.id''', (pid,)).fetchall()
        if not product:
            await q.edit_message_text('Sản phẩm không còn tồn tại.', reply_markup=inline_back('adm:stock'))
            return
        txt = f"📦 KHO SẢN PHẨM: {product['name']}\nChọn thời hạn để xem và quản lý key:"
        rows_kb = [[InlineKeyboardButton(
            f"🔑 {r['duration_label']} · Còn {r['available']}/{r['total']}",
            callback_data=f"adm:stockopt:{r['id']}"
        )] for r in rows]
        rows_kb.append([InlineKeyboardButton('↩️ Quay lại', callback_data=f"adm:stockcat:{product['category_id']}")])
        await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(rows_kb))
    elif data.startswith('adm:stockopt:'):
        oid = int(data.split(':')[2])
        with db() as con:
            row = con.execute('''SELECT po.id,po.product_id,po.duration_label,po.price,po.sell_price,
                p.name AS product_name,
                COUNT(pk.id) FILTER (WHERE pk.status='available') AS available,
                COUNT(pk.id) FILTER (WHERE pk.status='sold') AS sold,
                COUNT(pk.id) AS total
                FROM product_options po JOIN products p ON p.id=po.product_id
                LEFT JOIN product_keys pk ON pk.option_id=po.id
                WHERE po.id=? GROUP BY po.id,p.name''', (oid,)).fetchone()
        if not row:
            await q.edit_message_text('Gói thời hạn không còn tồn tại.', reply_markup=inline_back('adm:stock'))
            return
        txt = (f"📦 KHO KEY\n🎮 Sản phẩm: {row['product_name']}\n"
               f"⏳ Thời hạn: {row['duration_label']}\n"
               f"🟢 Còn trong kho: {row['available']}\n🔴 Đã bán: {row['sold']}\n"
               f"📊 Tổng key: {row['total']}\n💰 Giá thường: {row['price']:,}đ | Sell: {row['sell_price']:,}đ")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('🗑 Xóa tất cả key', callback_data=f'adm:stockdelete:{oid}')],
            [InlineKeyboardButton('📄 Gửi tất cả key (.txt)', callback_data=f'adm:stockexport:{oid}')],
            [InlineKeyboardButton('↩️ Quay lại thời hạn', callback_data=f"adm:stockprod:{row['product_id']}")],
        ])
        await q.edit_message_text(txt, reply_markup=kb)
    elif data.startswith('adm:stockdelete:'):
        oid = int(data.split(':')[2])
        with db() as con:
            row = con.execute('SELECT po.duration_label,p.name AS product_name FROM product_options po JOIN products p ON p.id=po.product_id WHERE po.id=?', (oid,)).fetchone()
        if not row:
            await q.edit_message_text('Gói thời hạn không còn tồn tại.', reply_markup=inline_back('adm:stock'))
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('⚠️ XÁC NHẬN XÓA TẤT CẢ', callback_data=f'adm:stockdeleteconfirm:{oid}')],
            [InlineKeyboardButton('Hủy', callback_data=f'adm:stockopt:{oid}')],
        ])
        await q.edit_message_text(
            f"Bạn chắc chắn muốn xóa toàn bộ key của {row['product_name']} – {row['duration_label']}?\n"
            'Thao tác này xóa cả key còn trong kho và key đã bán, không thể hoàn tác.',
            reply_markup=kb
        )
    elif data.startswith('adm:stockdeleteconfirm:'):
        oid = int(data.split(':')[2])
        with db() as con:
            row = con.execute('SELECT po.product_id,po.duration_label,p.name AS product_name FROM product_options po JOIN products p ON p.id=po.product_id WHERE po.id=?', (oid,)).fetchone()
            if not row:
                await q.edit_message_text('Gói thời hạn không còn tồn tại.', reply_markup=inline_back('adm:stock'))
                return
            result = con.execute('DELETE FROM product_keys WHERE option_id=?', (oid,))
            deleted = result.rowcount
        await q.edit_message_text(
            f"✅ Đã xóa {deleted} key của {row['product_name']} – {row['duration_label']}.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('↩️ Quay lại thời hạn', callback_data=f"adm:stockprod:{row['product_id']}")],
                [InlineKeyboardButton('🏠 Admin', callback_data='adm:menu')],
            ])
        )
    elif data.startswith('adm:stockexport:'):
        oid = int(data.split(':')[2])
        with db() as con:
            row = con.execute('SELECT po.duration_label,p.name AS product_name FROM product_options po JOIN products p ON p.id=po.product_id WHERE po.id=?', (oid,)).fetchone()
            keys = con.execute("SELECT key_value FROM product_keys WHERE option_id=? AND status='available' ORDER BY id", (oid,)).fetchall()
        if not row:
            await q.edit_message_text('Gói thời hạn không còn tồn tại.', reply_markup=inline_back('adm:stock'))
            return
        if not keys:
            await q.answer('Thời hạn này chưa có key.', show_alert=True)
            return
        safe_name = re.sub(r'[^A-Za-z0-9_-]+', '_', f"{row['product_name']}_{row['duration_label']}").strip('_')[:60] or 'keys'
        content = ('\n'.join(k['key_value'] for k in keys) + '\n').encode('utf-8')
        file_obj = io.BytesIO(content)
        file_obj.name = f'{safe_name}.txt'
        try:
            await context.bot.send_document(
                chat_id=user.id,
                document=InputFile(file_obj, filename=file_obj.name),
                caption=f"📄 Danh sách key còn trong kho: {row['product_name']} – {row['duration_label']}\nTổng key chưa bán: {len(keys)}."
            )
            if q.message and q.message.chat.type != 'private':
                await q.message.reply_text('📬 File kho key đã được gửi riêng cho tài khoản admin.')
        except Exception:
            if q.message:
                await q.message.reply_text(
                    'Không gửi được file kho key. Hãy mở chat riêng với bot rồi thử xuất kho lại.'
                )
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
            dep = con.execute('SELECT d.*,u.username,u.full_name FROM deposits d JOIN users u ON u.user_id=d.user_id WHERE d.id=? FOR UPDATE OF d', (deposit_id,)).fetchone()
            if not dep:
                await q.edit_message_text('Không tìm thấy đơn nạp tiền.'); return
            if dep['status'] not in ('pending', 'submitted'):
                await q.edit_message_text(f"Đơn #{deposit_id} đã được xử lý trước đó: {dep['status']}."); return
            if action == 'approve':
                con.execute('UPDATE users SET balance=balance+? WHERE user_id=?', (dep['amount'], dep['user_id']))
                row = con.execute('SELECT balance FROM users WHERE user_id=?', (dep['user_id'],)).fetchone()
                con.execute("INSERT INTO balance_transactions(user_id,admin_id,action,amount,balance_after,note,created_at) VALUES(?,?,'deposit',?,?,?,?)", (dep['user_id'], q.from_user.id, dep['amount'], row['balance'], f"Duyệt nạp #{deposit_id}", now()))
                con.execute("UPDATE deposits SET status='approved' WHERE id=?", (deposit_id,))
                username_text = '@' + dep['username'] if dep['username'] else 'Không có username'
                result = (f"🎉 DUYỆT NẠP TIỀN THÀNH CÔNG!\n\n🧾 Đơn thứ tự: #{deposit_id}\n💰 Đã cộng: {dep['amount']:,}đ\n👤 Người dùng: {escape(dep['full_name'] or 'Không có tên')}\n📛 Username: {escape(username_text)}\n🆔 ID: <code>{dep['user_id']}</code>\n💳 Số dư hiện tại: {row['balance']:,}đ")
                username_notice = '@' + dep['username'] if dep['username'] else 'Không có'
                user_notice = (
                    'NẠP TIỀN THÀNH CÔNG\n'
                    f"ID: {dep['user_id']}\n"
                    f"Username: {username_notice}\n"
                    f"Full name: {dep['full_name'] or 'Không có'}\n"
                    f"+{dep['amount']:,}đ\n"
                    f"ND: {dep['note'] or ''}"
                )
            else:
                con.execute("UPDATE deposits SET status='rejected' WHERE id=?", (deposit_id,))
                result = f"❌ Đã từ chối đơn nạp #{deposit_id}."
                user_notice = f"❌ Yêu cầu nạp tiền #{deposit_id} đã bị từ chối. Nếu bạn đã chuyển tiền, vui lòng liên hệ admin."
        await q.edit_message_text(result, parse_mode='HTML' if action == 'approve' else None)
        try: await context.bot.send_message(chat_id=dep['user_id'], text=user_notice)
        except Exception: log.exception('Cannot notify user about deposit %s', deposit_id)
    except Exception:
        log.exception('Deposit review failed for %s', deposit_id)
        await q.message.reply_text('Có lỗi khi xử lý đơn nạp. Kiểm tra Logs trước khi thao tác lại.')

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None:
        return
    text = (msg.text or '').strip(); flow = context.user_data.get('flow'); user = update.effective_user
    if text == '↩️ Trở về':
        context.user_data.clear()
        await msg.reply_text('✅ Đã gỡ nút Trở về cũ.', reply_markup=ReplyKeyboardRemove())
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
        preview_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('✅ Xác nhận gửi', callback_data='adm:broadcast:confirm'),
             InlineKeyboardButton('❌ Hủy', callback_data='adm:broadcast:cancel')]
        ])

        if getattr(msg, 'forward_origin', None) is not None:
            bc_payload = {
                'kind': 'forward',
                'from_chat_id': msg.chat_id,
                'message_id': msg.message_id,
            }
            context.user_data['pending_broadcast'] = bc_payload
            context.user_data['flow'] = None
            await context.bot.forward_message(chat_id=msg.chat_id, from_chat_id=msg.chat_id, message_id=msg.message_id)
            await msg.reply_text('📢 <b>XEM TRƯỚC THÔNG BÁO CHUYỂN TIẾP</b> (giữ nguyên nguồn & kiểu chữ):', parse_mode='HTML', reply_markup=preview_kb)
            return

        if msg.sticker:
            sticker_id = msg.sticker.file_id
            bc_payload = {
                'kind': 'sticker',
                'sticker': sticker_id,
            }
            context.user_data['pending_broadcast'] = bc_payload
            context.user_data['flow'] = None
            await msg.reply_sticker(sticker=sticker_id)
            await msg.reply_text('📢 <b>XEM TRƯỚC THÔNG BÁO NHÃN DÁN:</b>', parse_mode='HTML', reply_markup=preview_kb)
            return

        if msg.photo:
            photo_file_id = msg.photo[-1].file_id
            bc_text = msg.caption or ''
            bc_entities = msg.caption_entities or None
            bc_payload = {
                'kind': 'photo',
                'text': bc_text,
                'photo': photo_file_id,
                'entities': bc_entities,
            }
            context.user_data['pending_broadcast'] = bc_payload
            context.user_data['flow'] = None
            await msg.reply_photo(
                photo=photo_file_id,
                caption=bc_text or '📢 Xem trước thông báo ảnh',
                caption_entities=bc_entities,
                reply_markup=preview_kb,
            )
            return

        if msg.text is not None:
            bc_entities = msg.entities or None
            bc_payload = {
                'kind': 'text',
                'text': msg.text,
                'entities': bc_entities,
            }
            context.user_data['pending_broadcast'] = bc_payload
            context.user_data['flow'] = None
            await msg.reply_text(
                msg.text,
                entities=bc_entities,
                reply_markup=preview_kb,
            )
            return

        bc_payload = {
            'kind': 'copy',
            'from_chat_id': msg.chat_id,
            'message_id': msg.message_id,
        }
        context.user_data['pending_broadcast'] = bc_payload
        context.user_data['flow'] = None
        await context.bot.copy_message(chat_id=msg.chat_id, from_chat_id=msg.chat_id, message_id=msg.message_id)
        await msg.reply_text('📢 <b>XEM TRƯỚC THÔNG BÁO:</b>', parse_mode='HTML', reply_markup=preview_kb)
        return

    if flow == 'custom_quantity':
        if not text.isdigit() or not 1 <= int(text) <= 1000: await msg.reply_text('Nhập số nguyên từ 1 đến 1.000.'); return
        oid = context.user_data.get('selected_option'); context.user_data['flow'] = None
        with db() as con:
            o = con.execute('SELECT po.*,p.name AS product_name,p.category_id,c.name AS category_name FROM product_options po JOIN products p ON p.id::text=po.product_id::text JOIN categories c ON c.id::text=p.category_id::text WHERE po.id=? AND p.is_active=TRUE', (oid,)).fetchone()
            u_row = con.execute('SELECT is_seller FROM users WHERE user_id=?',(user.id,)).fetchone()
        if not o: await msg.reply_text('Gói sản phẩm không còn tồn tại.', reply_markup=USER_KB); return
        is_seller = bool(u_row and u_row['is_seller'])
        current_price = o['sell_price'] if (is_seller and o['sell_price'] > 0) else o['price']
        qty = int(text); context.user_data['pending_purchase'] = {'option_id': oid, 'quantity': qty, 'total': current_price * qty}
        p = context.user_data['pending_purchase']
        payment_note = ('Bot sẽ trừ số dư nội bộ; Shopyuri cấp key.' if o.get('shopyuri_product_id') else 'Bot sẽ kiểm tra kho và số dư trước khi trừ tiền.')
        await msg.reply_text(f"🧾 XÁC NHẬN MUA HÀNG\n\n📁 Danh mục: {escape(o['category_name'])}\n📦 Sản phẩm: {escape(o['product_name'])}\n⏳ Thời hạn: {escape(o['duration_label'])}\n🔢 Số lượng: {p['quantity']}\n💰 Giá bán: {p['total']:,}đ\n\n{payment_note}", parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[quick_button('🛍 XÁC NHẬN MUA HÀNG', callback_data='shop:confirm')], [InlineKeyboardButton('❌ HỦY', callback_data='shop:cancel')], [InlineKeyboardButton('↩️ Trở về', callback_data='menu:home')]])); return
    if flow == 'deposit_amount':
        if not text.isdigit() or not 1 <= int(text) <= 1_000_000_000:
            await msg.reply_text('Số tiền không hợp lệ. Nhập số nguyên từ 1 đến 1.000.000.000 VNĐ:'); return
        if not QR_ACCOUNT:
            await msg.reply_text('Shop chưa cấu hình tài khoản nhận tiền. Vui lòng liên hệ Admin.')
            context.user_data['flow'] = None
            return
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
    if flow == 'add_product_duration' and is_admin(update):
        label = text.strip()
        pid = context.user_data.get('duration_product_id')
        if not pid or not label or len(label) > 40 or not re.search(r'\d', label):
            await msg.reply_text('Thời hạn không hợp lệ. Nhập lại, ví dụ: 12 giờ hoặc 2 ngày:'); return
        try:
            with db() as con:
                exists = con.execute('SELECT 1 FROM product_options WHERE product_id=? AND LOWER(duration_label)=LOWER(?)', (pid,label)).fetchone()
                if exists:
                    await msg.reply_text('Sản phẩm đã có thời hạn này. Nhập thời hạn khác:'); return
                code = f"custom_{int(time.time() * 1000)}"
                con.execute('INSERT INTO product_options(product_id,duration,duration_label,price,sell_price) VALUES(?,?,?,0,0)', (pid,code,label))
            context.user_data.clear()
            await msg.reply_text(f'✅ Đã thêm thời hạn {label}. Thời hạn mới đã được đồng bộ lên Mini App với giá mặc định 0đ.', reply_markup=ADMIN_KB)
        except Exception:
            log.exception('Add custom product duration failed')
            context.user_data.clear(); await msg.reply_text('❌ Không thể thêm thời hạn. Kiểm tra Logs.', reply_markup=ADMIN_KB)
        return
    if flow == 'rename_option_duration' and is_admin(update):
        label = text.strip()
        oid = context.user_data.get('rename_option_id')
        pid = context.user_data.get('rename_option_product_id')
        if not oid or not pid or not label or len(label) > 40 or not re.search(r'\d', label):
            await msg.reply_text('Thời hạn không hợp lệ. Nhập lại, ví dụ: 1 ngày hoặc 12 giờ:'); return
        try:
            with db() as con:
                current = con.execute('SELECT id,duration FROM product_options WHERE id=? AND product_id=?', (oid,pid)).fetchone()
                if not current:
                    raise ValueError('Không tìm thấy gói thời hạn.')
                duplicate = con.execute('SELECT 1 FROM product_options WHERE product_id=? AND id<>? AND LOWER(duration_label)=LOWER(?)', (pid,oid,label)).fetchone()
                if duplicate:
                    await msg.reply_text('Sản phẩm đã có thời hạn này. Nhập thời hạn khác:'); return
                normalized = label.lower().replace(' ', '')
                known = {'1gio':'1h','1giờ':'1h','12gio':'12h','12giờ':'12h','1ngay':'1d','1ngày':'1d','7ngay':'7d','7ngày':'7d','30ngay':'30d','30ngày':'30d'}
                code = known.get(normalized, f"custom_{int(time.time()*1000)}")
                conflict = con.execute('SELECT 1 FROM product_options WHERE product_id=? AND id<>? AND duration=?', (pid,oid,code)).fetchone()
                if conflict:
                    code = f"custom_{int(time.time()*1000)}"
                con.execute('UPDATE product_options SET duration=?,duration_label=? WHERE id=?', (code,label,oid))
            context.user_data.clear()
            await msg.reply_text(f'✅ Đã đổi thời hạn thành {label}. Mini App sẽ hiển thị thời hạn mới.', reply_markup=ADMIN_KB)
        except Exception as exc:
            log.exception('Rename product duration failed')
            await msg.reply_text(f'❌ Không thể đổi thời hạn: {str(exc)[:180]}')
        return
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
                con.execute("INSERT INTO product_options(product_id,duration,duration_label,price,sell_price) VALUES(?,'1h','1 giờ',0,0),(?,'1d','1 ngày',0,0)", (p['id'], p['id']))
            context.user_data.clear()
            await msg.reply_text(f'✅ Thêm sản phẩm "{text}" thành công!\n\nĐã tạo các gói thời hạn mặc định.', reply_markup=ADMIN_KB)
        except Exception:
            log.exception('Add product failed')
            context.user_data.clear(); await msg.reply_text('❌ Không thể thêm sản phẩm. Kiểm tra Logs.', reply_markup=ADMIN_KB)
        return
    if flow == 'map_shopyuri_id' and is_admin(update):
        if not text.isdigit():
            await msg.reply_text('ID Shopyuri phải là số. Nhập lại product_id:'); return
        oid = context.user_data.get('shopyuri_target_option')
        try:
            with db() as con: con.execute('UPDATE product_options SET shopyuri_product_id=? WHERE id=?', (text, oid))
            context.user_data.clear(); await msg.reply_text(f'✅ Đã gán ID Shopyuri {text} cho gói sản phẩm.', reply_markup=ADMIN_KB)
        except Exception:
            log.exception('Map Shopyuri option failed'); context.user_data.clear()
            await msg.reply_text('❌ Không thể gán ID Shopyuri. Kiểm tra Logs.', reply_markup=ADMIN_KB)
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
            info = con.execute('SELECT p.name AS product_name,c.name AS category_name,po.duration_label FROM product_options po JOIN products p ON p.id::text=po.product_id::text JOIN categories c ON c.id::text=p.category_id::text WHERE po.id=? AND p.is_active=TRUE', (oid,)).fetchone()
            stock = con.execute("SELECT COUNT(*) AS n FROM product_keys WHERE option_id=? AND status='available'", (oid,)).fetchone()['n']
        context.user_data.clear()
        await msg.reply_text(f'✅ Đã thêm {added}/{len(keys)} key thành công. Kho hiện có: {stock} key.', reply_markup=ADMIN_KB)
        if added and info:
            announcement = (f"🔥 CÓ HÀNG MỚI VỀ! 🔥\n━━━━━━━━━━━━━━━━━━\n📁 Danh mục: {escape(info['category_name'])}\n🎮 Sản phẩm: {escape(info['duration_label'])}\n🔢 Số lượng: {added} sản phẩm đã được cập nhật!\n\n👉 Nhấn nút bên dưới để mở Mini App và mua ngay!")
            shop_markup = InlineKeyboardMarkup([[quick_button('🛍 MỞ MINI APP – MUA HÀNG', web_app=WebAppInfo(url=MINI_APP_URL))]]) if MINI_APP_URL.startswith('https://') else None
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
    retryable_statuses = {500, 502, 503, 504}
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                result=json.loads(response.read().decode('utf-8'))
            if not result.get('ok'):
                raise RuntimeError(str(result.get('description') or 'Telegram API error'))
            return result.get('result')
        except urllib.error.HTTPError as exc:
            if exc.code not in retryable_statuses or attempt == 2:
                raise
            exc.close()
            log.warning(
                'Telegram API temporarily returned HTTP %s; retrying request (%s/3)',
                exc.code, attempt + 1,
            )
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt == 2:
                raise
            log.warning(
                'Telegram API request failed temporarily (%s); retrying request (%s/3)',
                type(exc).__name__, attempt + 1,
            )
        time.sleep(0.5 * (2 ** attempt))


async def broadcast_new_stock_http(text):
    with db() as con:
        users = con.execute('SELECT user_id FROM users WHERE is_locked=FALSE ORDER BY user_id').fetchall()
    reply_markup = None
    if MINI_APP_URL.startswith('https://'):
        reply_markup = {
            'inline_keyboard': [[{
                'text': '🛍 MỞ MINI APP – MUA HÀNG',
                'icon_custom_emoji_id': CUSTOM_BUTTON_EMOJI_ID,
                'web_app': {'url': MINI_APP_URL},
            }]]
        }
    sent = 0
    for user in users:
        payload = {'chat_id': user['user_id'], 'text': text}
        if reply_markup:
            payload['reply_markup'] = reply_markup
        try:
            await asyncio.to_thread(_telegram_http, 'sendMessage', payload)
            sent += 1
        except Exception as exc:
            log.info('Mini App stock announcement skipped a recipient (%s)', type(exc).__name__)
        await asyncio.sleep(0.04)
    return sent, len(users)


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
    html_path = None
    for candidate in ('index.html', 'index (8).html', 'index(8).html', 'index(3).html'):
        p = BASE_DIR / candidate
        if p.exists():
            html_path = p
            break
    if html_path is None:
        raise HTTPException(status_code=404, detail='Không tìm thấy index.html')
    return FileResponse(
        html_path,
        media_type='text/html; charset=utf-8',
        headers={
            'Cache-Control': 'private, no-store, no-cache, max-age=0, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0',
        },
    )


@web_app.get('/health')
async def health():
    return {'ok': True, 'service': 'jinny-cheat'}


@web_app.get('/api/me')
def api_me(request: Request):
    user = _mini_user(request)
    with db() as con:
        u = con.execute('SELECT balance,is_locked,is_seller FROM users WHERE user_id=?', (user['id'],)).fetchone()
        deposits = con.execute("SELECT id,amount,status,note,created_at FROM deposits WHERE user_id=? AND status='approved' ORDER BY id DESC LIMIT 30", (user['id'],)).fetchall()
        purchases = con.execute('SELECT id,category_name,product_name,duration_label,quantity,total_price,keys_text,created_at FROM purchases WHERE user_id=? ORDER BY id DESC LIMIT 30', (user['id'],)).fetchall()
        total_deposit = con.execute("SELECT COALESCE(SUM(amount),0) AS n FROM deposits WHERE user_id=? AND status='approved'", (user['id'],)).fetchone()['n']
        total_spent = con.execute('SELECT COALESCE(SUM(total_price),0) AS n FROM purchases WHERE user_id=?', (user['id'],)).fetchone()['n']
        icon_row=con.execute("SELECT setting_value FROM bot_settings WHERE setting_key='miniapp_ff_icon'").fetchone()
    return {'user_id': user['id'], 'balance': int(u['balance']), 'is_admin': user['id'] in ADMIN_IDS,
            'is_locked': bool(u['is_locked']), 'is_seller': bool(u['is_seller']),
            'ff_app_icon': icon_row['setting_value'] if icon_row else '',
            'total_deposit': int(total_deposit), 'total_spent': int(total_spent),
            'deposits': deposits, 'purchases': purchases}


def _build_shopyuri_catalog_for_user(user_id):
    with db() as con:
        cats = con.execute('''SELECT DISTINCT c.id,c.name,c.guide_url,c.icon
            FROM categories c
            JOIN products p ON p.category_id::text=c.id::text
            JOIN product_options po ON po.product_id::text=p.id::text
            WHERE p.is_active=TRUE
            ORDER BY c.id''').fetchall()
        output=[]
        seller = con.execute('SELECT is_seller FROM users WHERE user_id=?', (user_id,)).fetchone()
        is_seller = bool(seller and seller['is_seller'])
        for cat in cats:
            products = con.execute('''SELECT DISTINCT p.id,p.name FROM products p
                JOIN product_options po ON po.product_id::text=p.id::text
                WHERE p.category_id::text=?::text AND p.is_active=TRUE ORDER BY p.id''', (cat['id'],)).fetchall()
            prod_out=[]
            for product in products:
                opts=con.execute('''SELECT po.id,po.duration,po.duration_label,po.price,po.sell_price,
                    po.shopyuri_product_id,po.shopyuri_stock,
                    (SELECT COUNT(*) FROM product_keys pk
                     WHERE pk.option_id=po.id AND pk.status='available') AS local_stock
                    FROM product_options po
                    WHERE po.product_id::text=?::text
                    ORDER BY po.id''',(str(product['id']),)).fetchall()
                prod_out.append({'id':product['id'],'name':product['name'],'options':[{
                    'id':o['id'],'duration':o['duration'],'duration_label':o['duration_label'],
                    'price':int(o['sell_price'] if (is_seller and o['sell_price'] and o['sell_price'] > 0) else o['price']),
                    'stock':(int(o['shopyuri_stock']) if o['shopyuri_stock'] is not None else 0)
                            if o['shopyuri_product_id'] else int(o['local_stock']),
                    'stock_source':'supplier' if o['shopyuri_product_id'] else 'local',
                    'regular_price':int(o['price'] or 0),
                    'sell_price':int(o['sell_price'] or 0),
                    'shopyuri':bool(o['shopyuri_product_id']),
                    'shopyuri_product_id':str(o['shopyuri_product_id']) if o['shopyuri_product_id'] else None
                } for o in opts]})
            if prod_out:
                output.append({'id':cat['id'],'name':cat['name'],'guide_url':cat['guide_url'],
                               'icon':cat['icon'] or '', 'products':prod_out})
    return output

async def _refresh_shopyuri_catalog_background():
    try:
        await asyncio.to_thread(_sync_shopyuri_catalog, False)
    except Exception:
        log.exception('Background Shopyuri catalog refresh failed')


@web_app.get('/api/catalog')
async def api_catalog(request: Request):
    user = _mini_user(request)
    cached = await asyncio.to_thread(_build_shopyuri_catalog_for_user, user['id'])
    has_snapshot = any(cat.get('products') for cat in cached)
    force = request.query_params.get('refresh') == '1'
    if has_snapshot and not force:
        if not _SHOPYURI_LAST_SYNC or time.time() - _SHOPYURI_LAST_SYNC >= 45:
            asyncio.create_task(_refresh_shopyuri_catalog_background())
        return {'categories': cached, 'cached': True, 'refreshing': True}

    try:
        await asyncio.to_thread(_sync_shopyuri_catalog, force or not has_snapshot)
    except Exception as exc:
        log.exception('Shopyuri catalog refresh failed')
        if not has_snapshot:
            raise HTTPException(status_code=502, detail=str(exc)[:1000])
        log.warning('Serving last successful Shopyuri catalog snapshot after refresh error: %s', exc)
    fresh = await asyncio.to_thread(_build_shopyuri_catalog_for_user, user['id'])
    return {'categories': fresh, 'cached': False, 'refreshing': False}


@web_app.post('/api/purchases')
async def api_purchase(request: Request):
    user = _mini_user(request)
    payload = await request.json()
    raw_oid = str(payload.get('option_id') or '').strip()
    try:
        qty = int(payload.get('quantity'))
        if qty < 1 or qty > 100:
            raise ValueError()
        oid = int(raw_oid)
    except Exception:
        raise HTTPException(status_code=400, detail='Số lượng hoặc gói sản phẩm không hợp lệ.')

    supplier_order=''; key_text=''; con=db()
    try:
        con.execute('BEGIN')
        u=con.execute('SELECT balance,is_locked,is_seller FROM users WHERE user_id=? FOR UPDATE',(user['id'],)).fetchone()
        opt=con.execute('''SELECT po.*,p.name AS product_name,p.category_id,c.name AS category_name FROM product_options po
            JOIN products p ON p.id::text=po.product_id::text JOIN categories c ON c.id::text=p.category_id::text WHERE po.id=? AND p.is_active=TRUE''',(oid,)).fetchone()
        if not u or u['is_locked']: raise HTTPException(status_code=403,detail='Tài khoản không thể mua hàng.')
        if not opt: raise HTTPException(status_code=404,detail='Gói sản phẩm không tồn tại.')
        price=opt['sell_price'] if u['is_seller'] and opt['sell_price']>0 else opt['price']; subtotal=int(price)*qty
        voucher_code=str(payload.get('voucher_code') or '').strip()
        coupon=None; discount=0
        if voucher_code:
            coupon=con.execute('SELECT * FROM discount_coupons WHERE code=? AND is_active=TRUE FOR UPDATE',(voucher_code,)).fetchone()
            if not coupon or int(coupon['used_count']) >= int(coupon['max_uses']):
                raise HTTPException(status_code=400,detail='Mã giảm giá không hợp lệ hoặc đã hết lượt sử dụng.')
            discount=(subtotal*int(coupon['discount_percent']))//100
        total=max(0,subtotal-discount)
        external=bool(opt.get('shopyuri_product_id'))
        actual_supplier_id = str(opt.get('shopyuri_product_id') or '').strip()
        if external and not actual_supplier_id.isdigit():
            raise HTTPException(status_code=409, detail='Gói sản phẩm chưa có ID Shopyuri hợp lệ. Hãy đồng bộ lại danh sách.')
        if u['balance'] < total:
            raise HTTPException(status_code=400,detail='⚠️ Hệ thống đang gặp sự cố xử lý đơn hàng. Vui lòng chờ một chút hoặc nhắn tin Admin để được hỗ trợ.')
        if external:
            try:
                log.info('Mini App purchase: user=%s option_id=%s supplier_product_id=%s qty=%s', user['id'], oid, actual_supplier_id, qty)
                key_text,supplier_order=await asyncio.to_thread(shopyuri_buy,actual_supplier_id,qty)
            except Exception as api_error:
                log.warning('Mini App Shopyuri purchase failed for user=%s product=%s: %s',
                            user['id'], opt['shopyuri_product_id'], api_error)
                raise HTTPException(
                    status_code=502,
                    detail='⚠️ Hệ thống đang gặp sự cố xử lý đơn hàng. Vui lòng chờ một chút hoặc nhắn tin Admin để được hỗ trợ. Không tự mua lại nếu đơn trước chưa được Admin kiểm tra.'
                )
        else:
            keys=con.execute("SELECT id,key_value FROM product_keys WHERE option_id=? AND status='available' ORDER BY id LIMIT ? FOR UPDATE SKIP LOCKED",(oid,qty)).fetchall()
            if len(keys)<qty: raise HTTPException(status_code=400,detail=f'Kho không đủ key. Hiện còn {len(keys)} key.')
            key_text='\n'.join(k['key_value'] for k in keys)
        con.execute('UPDATE users SET balance=balance-? WHERE user_id=?',(total,user['id']))
        if not external:
            for k in keys: con.execute("UPDATE product_keys SET status='sold',sold_to=?,sold_at=? WHERE id=?",(user['id'],now(),k['id']))
        purchase_row=con.execute('''INSERT INTO purchases(user_id,category_name,product_name,duration_label,quantity,unit_price,total_price,keys_text,created_at,supplier_order_code)
            VALUES(?,?,?,?,?,?,?,?,?,?) RETURNING id''',(user['id'],opt['category_name'],opt['product_name'],opt['duration_label'],qty,price,total,key_text,now(),supplier_order or None)).fetchone()
        if coupon:
            con.execute('UPDATE discount_coupons SET used_count=used_count+1 WHERE id=? AND used_count < max_uses',(coupon['id'],))
        purchase_id=int(purchase_row['id'])
        con.commit()
    except HTTPException:
        con.rollback(); raise
    except Exception:
        con.rollback()
        log.exception('Mini App purchase failed')
        raise HTTPException(status_code=500,detail='Không thể xử lý đơn hàng lúc này. Vui lòng thử lại hoặc liên hệ Admin.')
    finally: con.close()
    guide_url = None
    try:
        with db() as guide_con:
            guide_row = guide_con.execute('SELECT guide_url FROM categories WHERE id=?', (opt['category_id'],)).fetchone()
            guide_url = guide_row['guide_url'] if guide_row else None
        telegram_text = (
            "🎉 MUA HÀNG THÀNH CÔNG! 🎉\n\n"
            f"🎮 Game: {escape(opt['category_name'])}\n📦 Sản phẩm: {escape(opt['product_name'])} - {escape(opt['duration_label'])}\n"
            f"🔢 Số lượng: {qty}\n💰 Tổng tiền: {total:,}đ\n\n🔑 KEY CỦA BẠN:\n<pre>{escape(key_text)}</pre>"
        )
        await asyncio.to_thread(
            _telegram_http,
            'sendMessage',
            {'chat_id':user['id'],'text':telegram_text,'parse_mode':'HTML'},
        )
    except Exception: log.exception('Could not send purchased keys to Telegram user %s',user['id'])
    return {'success':True,'purchase_id':purchase_id,'total':total,'total_price':total,'keys_text':key_text,
            'category_name':opt['category_name'],'product_name':opt['product_name'],'duration_label':opt['duration_label'],
            'quantity':qty,'discount':discount,'guide_url':guide_url,'message':'Mua hàng thành công.'}


@web_app.post('/webhook/sepay')
async def sepay_webhook(request: Request):
    if not SEPAY_WEBHOOK_API_KEY:
        raise HTTPException(status_code=503, detail='Webhook API key is not configured.')
    auth = request.headers.get('authorization', '').strip()
    expected = 'Apikey ' + SEPAY_WEBHOOK_API_KEY
    if not hmac.compare_digest(auth, expected):
        raise HTTPException(status_code=401, detail='Unauthorized')
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail='Invalid JSON')
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail='Invalid payload')
    if str(payload.get('transferType', '')).lower() not in ('in', 'in_only', 'credit', ''):
        return {'success': True, 'ignored': True, 'reason': 'not incoming'}
    try:
        amount = int(float(payload.get('transferAmount') or payload.get('amount') or 0))
        txid = str(payload.get('id') or payload.get('transactionId') or '').strip()
        content = str(payload.get('content') or payload.get('description') or '').upper()
    except Exception:
        raise HTTPException(status_code=400, detail='Invalid transaction fields')
    if amount <= 0 or not txid or not content:
        raise HTTPException(status_code=400, detail='Missing transaction id, amount or content')
    match = re.search(r'SEVQR(\d+)NAP(\d+)', content)
    if not match:
        return {'success': True, 'matched': False}
    uid, did = int(match.group(1)), int(match.group(2))
    await asyncio.sleep(SEPAY_AUTO_APPROVE_DELAY)
    approved = None
    with db() as con:
        con.execute('BEGIN')
        claimed = con.execute('INSERT INTO sepay_webhook_events(transaction_id,deposit_id,received_at,payload) VALUES(?,?,?,?::jsonb) ON CONFLICT DO NOTHING RETURNING transaction_id', (txid, did, now(), json.dumps(payload, ensure_ascii=False))).fetchone()
        if not claimed:
            con.rollback()
            return {'success': True, 'duplicate': True}
        dep = con.execute('SELECT * FROM deposits WHERE id=? AND user_id=? FOR UPDATE', (did, uid)).fetchone()
        if not dep or int(dep['amount']) != amount or str(dep['note'] or '').upper() != match.group(0):
            con.commit()
            return {'success': True, 'matched': False}
        if dep['status'] not in ('pending', 'submitted'):
            con.commit()
            return {'success': True, 'already_processed': True}
        con.execute('UPDATE users SET balance=balance+? WHERE user_id=?', (amount, uid))
        balance = con.execute('SELECT balance,username,full_name FROM users WHERE user_id=?', (uid,)).fetchone()
        con.execute("INSERT INTO balance_transactions(user_id,admin_id,action,amount,balance_after,note,created_at) VALUES(?,0,'deposit',?,?,?,?)", (uid, amount, balance['balance'], f'SePay tự duyệt #{did} · {txid}', now()))
        con.execute("UPDATE deposits SET status='approved',sepay_transaction_id=? WHERE id=?", (txid, did))
        approved = {'uid': uid, 'amount': amount, 'balance': balance['balance'], 'username': balance['username'], 'full_name': balance['full_name'], 'note': dep['note'], 'deposit_id': did}
        con.commit()
    if approved:
        uname = '@' + approved['username'] if approved['username'] else 'Không có'
        notice = (f"NẠP TIỀN THÀNH CÔNG\nID: {approved['uid']}\nUsername: {uname}\n"
                  f"Full name: {approved['full_name'] or 'Không có'}\n+{approved['amount']:,}đ\nND: {approved['note']}\n"
                  f"Số dư: {approved['balance']:,}đ")
        try:
            await asyncio.to_thread(_telegram_http, 'sendMessage', {'chat_id': approved['uid'], 'text': notice})
        except Exception:
            log.exception('Could not notify user about SePay deposit %s', did)
        for aid in ADMIN_IDS:
            try:
                await asyncio.to_thread(_telegram_http, 'sendMessage', {'chat_id': aid, 'text': f'✅ SEPAY TỰ DUYỆT #{did}\nID: {uid}\nSố tiền: {amount:,}đ\nGiao dịch: {txid}'})
            except Exception:
                log.exception('Could not notify admin about SePay deposit %s', did)
    return {'success': True, 'approved': bool(approved), 'deposit_id': did}


@web_app.post('/api/deposits/{deposit_id}/cancel')
async def api_cancel_deposit(deposit_id: int, request: Request):
    user = _mini_user(request)
    with db() as con:
        dep = con.execute('SELECT status FROM deposits WHERE id=? AND user_id=? FOR UPDATE', (deposit_id, user['id'])).fetchone()
        if not dep:
            raise HTTPException(status_code=404, detail='Không tìm thấy mã nạp.')
        if dep['status'] != 'pending':
            raise HTTPException(status_code=409, detail='Mã đã gửi xác nhận hoặc đã được xử lý, không thể hủy.')
        con.execute("UPDATE deposits SET status='cancelled' WHERE id=?", (deposit_id,))
    return {'success': True, 'message': 'Đã hủy mã nạp.'}


@web_app.get('/api/deposits/active')
async def api_active_deposit(request: Request):
    user = _mini_user(request)
    with db() as con:
        dep = con.execute("SELECT id,amount,status,note,created_at FROM deposits WHERE user_id=? AND status IN ('pending','submitted') ORDER BY id DESC LIMIT 1", (user['id'],)).fetchone()
    if not dep:
        return {'active': False}
    try:
        created = dep['created_at']
        dt = datetime.fromisoformat(created.replace('Z', '+00:00')) if isinstance(created, str) else created
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        remaining = max(0, 300 - int((datetime.now(timezone.utc) - dt).total_seconds()))
    except Exception:
        remaining = 0
    if remaining <= 0:
        with db() as con:
            con.execute("UPDATE deposits SET status='expired' WHERE id=? AND status IN ('pending','submitted')", (dep['id'],))
        return {'active': False}
    return {'active': True, 'deposit_id': dep['id'], 'amount': dep['amount'], 'status': dep['status'],
            'transfer_content': dep['note'], 'qr_url': make_qr_url(dep['amount'], dep['note']) + '&v=' + str(dep['id']), 'expires_in': remaining}


@web_app.post('/api/vouchers/validate')
async def api_validate_voucher(request: Request):
    _mini_user(request)
    payload = await request.json()
    code = str(payload.get('code') or '').strip()
    with db() as con:
        row = con.execute('SELECT code,discount_percent,max_uses,used_count FROM discount_coupons WHERE code=? AND is_active=TRUE', (code,)).fetchone()
    if not row or int(row['used_count']) >= int(row['max_uses']):
        raise HTTPException(status_code=400, detail='Mã giảm giá không hợp lệ hoặc đã hết lượt sử dụng.')
    return {'valid': True, 'code': row['code'], 'discount_percent': row['discount_percent'], 'remaining': int(row['max_uses'])-int(row['used_count'])}


@web_app.post('/api/deposits')
async def api_create_deposit(request: Request):
    user=_mini_user(request); payload=await request.json()
    if not QR_ACCOUNT:
        raise HTTPException(status_code=503,detail='Shop chưa cấu hình tài khoản nhận tiền. Vui lòng liên hệ Admin.')
    try: amount=int(payload.get('amount'))
    except Exception: raise HTTPException(status_code=400,detail='Số tiền không hợp lệ.')
    if amount<2000 or amount>1000000000: raise HTTPException(status_code=400,detail='Số tiền nạp từ 2.000đ đến 1.000.000.000đ.')
    with db() as con:
        con.execute('SELECT user_id FROM users WHERE user_id=? FOR UPDATE',(user['id'],)).fetchone()
        con.execute('''UPDATE deposits SET status='expired'
            WHERE user_id=? AND status IN ('pending','submitted')
              AND created_at::timestamptz < NOW() - INTERVAL '5 minutes' ''',(user['id'],))
        recent=con.execute("SELECT id,status,amount,note,created_at FROM deposits WHERE user_id=? AND status IN ('pending','submitted') ORDER BY id DESC LIMIT 1",(user['id'],)).fetchone()
        if recent and int(recent['amount']) == amount:
            did=int(recent['id']); memo=str(recent['note'] or '')
            remaining=300
            try:
                created=recent['created_at']
                created_dt=datetime.fromisoformat(created.replace('Z','+00:00')) if isinstance(created,str) else created
                if created_dt.tzinfo is None: created_dt=created_dt.replace(tzinfo=timezone.utc)
                remaining=max(0,300-int((datetime.now(timezone.utc)-created_dt).total_seconds()))
            except Exception:
                pass
            return {'deposit_id':did,'amount':amount,'transfer_content':memo,
                    'qr_url':make_qr_url(amount,memo) + '&v=' + str(did),
                    'expires_in':remaining,'status':recent['status'],
                    'message':('Đơn nạp này đang chờ Admin duyệt.' if recent['status']=='submitted'
                               else 'Đang hiển thị lại mã QR của đơn này.')}
        if recent and recent['status']=='pending':
            con.execute("UPDATE deposits SET status='expired' WHERE id=? AND status='pending'",(recent['id'],))
        row=con.execute("INSERT INTO deposits(user_id,amount,status,note,created_at) VALUES(?,?,'pending','',?) RETURNING id",(user['id'],amount,now())).fetchone()
        did=int(row['id'])
        memo=f'{QR_MEMO_PREFIX}{user["id"]}{QR_MEMO_SUFFIX}{did}'
        con.execute('UPDATE deposits SET note=? WHERE id=?',(memo,did))
    return {'deposit_id':did,'amount':amount,'transfer_content':memo,
            'qr_url':make_qr_url(amount,memo) + '&v=' + str(did),
            'expires_in':300,'status':'pending'}


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
        products=con.execute('SELECT p.id,p.name,p.category_id,p.is_active,c.name AS category_name FROM products p JOIN categories c ON c.id=p.category_id ORDER BY p.id').fetchall()
        options=con.execute('SELECT po.id,po.product_id,po.duration_label,po.price,po.sell_price,po.shopyuri_product_id,p.name AS product_name FROM product_options po JOIN products p ON p.id=po.product_id ORDER BY po.id').fetchall()
        users=con.execute('SELECT COUNT(*) AS n FROM users').fetchone()['n']
        revenue=con.execute("SELECT COALESCE(SUM(amount),0) AS n FROM deposits WHERE status='approved'").fetchone()['n']
        balances=con.execute('SELECT COALESCE(SUM(balance),0) AS n FROM users').fetchone()['n']
        maint=con.execute("SELECT setting_value FROM bot_settings WHERE setting_key='maintenance'").fetchone()
    return {'categories':cats,'products':products,'options':options,'stats':{'users':users,'revenue':int(revenue),'balances':int(balances)},'maintenance':bool(maint and maint['setting_value']=='on')}


def _extract_shopyuri_catalog(payload):
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ('products', 'items', 'list', 'categories', 'data', 'result', 'response'):
        value = payload.get(key)
        if isinstance(value, list):
            rows = [x for x in value if isinstance(x, dict)]
            if rows:
                return rows
        if isinstance(value, dict):
            rows = _extract_shopyuri_catalog(value)
            if rows:
                return rows
    return []


def _shopyuri_get(obj, *keys, default=None):
    if not isinstance(obj, dict):
        return default
    for key in keys:
        value = obj.get(key)
        if value not in (None, ''):
            return value
    return default


def _normalize_shopyuri_catalog(items):
    grouped = {}

    def add_option(category_name, category_icon, product_name, option, product_id_fallback=None, stock_fallback=None):
        supplier_id = _shopyuri_get(option, 'product_id', 'item_id', 'sku_id')
        if supplier_id is None:
            supplier_id = product_id_fallback
        if supplier_id is None:
            supplier_id = _shopyuri_get(option, 'id')
        if supplier_id is None:
            return
        supplier_id = str(supplier_id).strip()
        if not supplier_id.isdigit():
            return
        category_name = str(category_name or 'Sản phẩm').strip()[:80] or 'Sản phẩm'
        product_name = str(product_name or _shopyuri_get(option, 'product_name', 'title', 'name', default='Sản phẩm')).strip()[:100]
        duration = str(_shopyuri_get(option, 'duration_label', 'duration', 'period', 'time', 'package', default='1 ngày')).strip()[:40]
        try:
            price = max(0, int(float(_shopyuri_get(option, 'price', 'retail_price', 'selling_price', 'amount', 'original_price', default=0))))
        except (TypeError, ValueError):
            price = 0
        raw_stock = _shopyuri_get(option, 'stock', 'stock_quantity', 'available_stock', 'stock_count', 'quantity_available', 'available_quantity', 'available_qty', 'stock_available', 'qty_available', 'available_count', 'remain_stock', 'remaining_stock', 'inventory', 'remain', 'remaining', 'in_stock', 'quantity', 'qty', 'available')
        if raw_stock is None:
            raw_stock = stock_fallback
        if isinstance(raw_stock, dict):
            raw_stock = _shopyuri_get(raw_stock, 'available', 'quantity', 'qty', 'count', 'stock', 'remaining')
        try:
            supplier_stock = max(0, int(float(raw_stock))) if raw_stock is not None else None
        except (TypeError, ValueError):
            supplier_stock = 1 if str(raw_stock).strip().lower() in ('true', 'yes', 'in_stock') else (0 if raw_stock is not None else None)
        category = grouped.setdefault(category_name, {
            'name': category_name,
            'icon': str(category_icon or ''),
            'products_map': {}
        })
        product = category['products_map'].setdefault(product_name, {'name': product_name, 'options': []})
        if not any(str(x['shopyuri_id']) == supplier_id for x in product['options']):
            product['options'].append({
                'shopyuri_id': supplier_id,
                'duration_label': duration,
                'price': price,
                'stock': supplier_stock,
            })

    for item in items:
        if not isinstance(item, dict):
            continue
        nested_products = item.get('products')
        if isinstance(nested_products, list):
            cat_name = _shopyuri_get(item, 'name', 'category_name', 'title', default='Sản phẩm')
            cat_icon = _shopyuri_get(item, 'icon', 'image', 'image_url', 'thumbnail', default='')
            for product in nested_products:
                if not isinstance(product, dict):
                    continue
                product_name = _shopyuri_get(product, 'name', 'title', 'product_name', default='Sản phẩm')
                options = _shopyuri_get(product, 'options', 'variants', 'packages', 'durations')
                if isinstance(options, list) and options:
                    for option in options:
                        add_option(cat_name, cat_icon, product_name, option,
                                   _shopyuri_get(product, 'product_id', 'id'),
                                   _shopyuri_get(product, 'stock', 'stock_quantity', 'available_stock', 'quantity_available', 'available_qty', 'qty_available'))
                else:
                    add_option(cat_name, cat_icon, product_name, product,
                               _shopyuri_get(product, 'product_id', 'id'))
            continue

        cat_value = _shopyuri_get(item, 'category_name', 'category', 'group_name', 'category_title', default='Sản phẩm')
        if isinstance(cat_value, dict):
            cat_name = _shopyuri_get(cat_value, 'name', 'title', default='Sản phẩm')
            cat_icon = _shopyuri_get(cat_value, 'icon', 'image', 'image_url', default='')
        else:
            cat_name = cat_value
            cat_icon = _shopyuri_get(item, 'category_icon', 'icon', 'image', 'image_url', default='')
        product_name = str(_shopyuri_get(item, 'product_name', 'name', 'title', default='Sản phẩm'))
        flat_option = dict(item)
        has_duration = _shopyuri_get(item, 'duration_label', 'duration', 'period', 'time', 'package') is not None
        if not has_duration and ' - ' in product_name:
            product_name, inferred_duration = [part.strip() for part in product_name.split(' - ', 1)]
            flat_option['duration_label'] = inferred_duration
        add_option(cat_name, cat_icon, product_name, flat_option,
                   _shopyuri_get(item, 'product_id', 'id'))

    result = []
    for category in grouped.values():
        products = []
        for product in category['products_map'].values():
            products.append({'name': product['name'], 'options': product['options']})
        if products:
            result.append({'name': category['name'], 'icon': category['icon'], 'products': products})
    return result


_SHOPYURI_SYNC_LOCK = threading.Lock()
_SHOPYURI_LAST_SYNC = 0.0
_SHOPYURI_LAST_ERROR = ''


def _request_shopyuri_catalog():
    if not SHOPYURI_API_TOKEN:
        raise RuntimeError('Thiếu SHOPYURI_API_TOKEN trong Railway Variables.')
    errors = []
    for action in ('products', 'list', 'categories', 'catalog', 'get_products', ''):
        url = SHOPYURI_API_URL.split('?', 1)[0]
        if action:
            url += '?action=' + action
        req = urllib.request.Request(url, method='GET', headers={
            'Authorization': f'Bearer {SHOPYURI_API_TOKEN}',
            'Accept': 'application/json',
            'User-Agent': 'HmeShopee-MiniApp/1.0',
        })
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                raw = response.read().decode('utf-8', errors='replace')
            payload = json.loads(raw)
            items = _extract_shopyuri_catalog(payload)
            normalized = _normalize_shopyuri_catalog(items)
            if normalized:
                return normalized, action or 'default'
            message = payload.get('message') if isinstance(payload, dict) else ''
            errors.append(f'{action or "default"}: không tìm thấy sản phẩm hợp lệ' + (f' ({message})' if message else ''))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode('utf-8', errors='replace')[:220]
            errors.append(f'{action or "default"}: HTTP {exc.code} {body}')
        except Exception as exc:
            errors.append(f'{action or "default"}: {str(exc)[:220]}')
    raise RuntimeError('Không lấy được danh sách sản phẩm Shopyuri. ' + ' | '.join(errors))


def _sync_shopyuri_catalog(force=False):
    global _SHOPYURI_LAST_SYNC, _SHOPYURI_LAST_ERROR
    with _SHOPYURI_SYNC_LOCK:
        if not force and _SHOPYURI_LAST_SYNC and time.time() - _SHOPYURI_LAST_SYNC < 45:
            return {'synced': True, 'cached': True}
        categories, action = _request_shopyuri_catalog()
        supplier_ids = []
        synced_products = 0
        synced_options = 0
        stamp = now()
        with db() as con:
            con.execute('BEGIN')
            old_product_ids = [r['product_id'] for r in con.execute(
                'SELECT DISTINCT product_id FROM product_options WHERE shophenry_product_id IS NOT NULL AND shopyuri_product_id IS NULL'
            ).fetchall()]
            con.execute('DELETE FROM product_options WHERE shophenry_product_id IS NOT NULL AND shopyuri_product_id IS NULL')

            for category in categories:
                category_name = str(category.get('name') or 'Sản phẩm').strip()[:80]
                if not category_name:
                    continue
                cat_row = con.execute(
                    'INSERT INTO categories(name,created_by,created_at,icon) VALUES(?,?,?,?) '
                    'ON CONFLICT(name) DO UPDATE SET icon=CASE WHEN EXCLUDED.icon IS NOT NULL AND EXCLUDED.icon<>\'\' THEN EXCLUDED.icon ELSE categories.icon END RETURNING id',
                    (category_name, 0, stamp, str(category.get('icon') or ''))
                ).fetchone()
                cid = cat_row['id']
                for product in category.get('products') or []:
                    product_name = str(product.get('name') or 'Sản phẩm').strip()[:100]
                    if not product_name:
                        continue
                    prod_row = con.execute(
                        'SELECT id FROM products WHERE category_id=? AND LOWER(name)=LOWER(?) ORDER BY id LIMIT 1',
                        (cid, product_name)
                    ).fetchone()
                    if prod_row:
                        pid = prod_row['id']
                    else:
                        pid = con.execute(
                            'INSERT INTO products(category_id,name,created_by,created_at) VALUES(?,?,?,?) RETURNING id',
                            (cid, product_name, 0, stamp)
                        ).fetchone()['id']
                        synced_products += 1
                    for option in product.get('options') or []:
                        sid = str(option.get('shopyuri_id') or '').strip()
                        if not sid.isdigit():
                            continue
                        supplier_ids.append(sid)
                        label = str(option.get('duration_label') or '1 ngày').strip()[:40]
                        price = max(0, int(option.get('price') or 0))
                        existing = con.execute(
                            'SELECT id FROM product_options WHERE shopyuri_product_id=? ORDER BY id LIMIT 1', (sid,)
                        ).fetchone()
                        duration_code = 'shopyuri_' + sid
                        if existing:
                            con.execute(
                                'UPDATE product_options SET product_id=?,duration=?,duration_label=?,shopyuri_stock=? WHERE id=?',
                                (pid, duration_code, label, option.get('stock'), existing['id'])
                            )
                        else:
                            con.execute(
                                'INSERT INTO product_options(product_id,duration,duration_label,price,sell_price,shopyuri_product_id,shopyuri_stock) VALUES(?,?,?,?,0,?,?)',
                                (pid, duration_code, label, price, sid, option.get('stock'))
                            )
                        synced_options += 1

            if not supplier_ids:
                raise RuntimeError('API Shopyuri không trả về product_id dạng số; không thay đổi danh mục hiện tại.')
            current_ids = [r['shopyuri_product_id'] for r in con.execute(
                'SELECT shopyuri_product_id FROM product_options WHERE shopyuri_product_id IS NOT NULL'
            ).fetchall()]
            stale = set(current_ids) - set(supplier_ids)
            if stale:
                stale_rows = con.execute(
                    'SELECT id,product_id FROM product_options WHERE shopyuri_product_id = ANY(?)', (list(stale),)
                ).fetchall()
                stale_product_ids = {r['product_id'] for r in stale_rows}
                con.execute('DELETE FROM product_options WHERE shopyuri_product_id = ANY(?)', (list(stale),))
                old_product_ids.extend(stale_product_ids)
            for pid in set(old_product_ids):
                con.execute('DELETE FROM products WHERE id=? AND NOT EXISTS (SELECT 1 FROM product_options WHERE product_id=?)', (pid, pid))
            con.commit()
        _SHOPYURI_LAST_SYNC = time.time()
        _SHOPYURI_LAST_ERROR = ''
        return {'synced': True, 'cached': False, 'action': action,
                'synced_products': synced_products, 'synced_options': synced_options}


@web_app.get('/api/admin/shopyuri/catalog')
async def api_admin_shopyuri_catalog(request: Request):
    _mini_user(request, True)
    try:
        result = await asyncio.to_thread(_sync_shopyuri_catalog, True)
        return {'success': True, **result}
    except Exception as exc:
        log.exception('Shopyuri catalog sync failed')
        raise HTTPException(status_code=502, detail=str(exc)[:1200])


@web_app.post('/api/admin/action')
async def api_admin_action(request: Request):
    admin=_mini_user(request,True); data=await request.json(); action=str(data.get('action',''))
    added_keys = 0
    stock_info = None
    add_keys_result = None
    try:
        with db() as con:
            if action=='set_ff_app_icon':
                image_data=str(data.get('image_data') or '')
                if not image_data.startswith('data:image/') or len(image_data)>2_500_000:
                    raise HTTPException(status_code=400,detail='Ảnh không hợp lệ hoặc vượt quá dung lượng cho phép.')
                stamp = now()
                con.execute('''INSERT INTO bot_settings(setting_key,setting_value,updated_at) VALUES('miniapp_ff_icon',?,?)
                    ON CONFLICT(setting_key) DO UPDATE SET setting_value=EXCLUDED.setting_value,updated_at=EXCLUDED.updated_at''',
                    (image_data,stamp))
                return {'success':True,'message':'Đã lưu ảnh Free Fire. Ảnh sẽ hiển thị cho tất cả người dùng.'}
            if action=='set_category_icon':
                category_id=int(data.get('category_id') or 0)
                category_name=str(data.get('category_name') or '').strip()
                image_data=str(data.get('image_data') or '')
                if not image_data.startswith('data:image/') or len(image_data)>2_500_000:
                    raise HTTPException(status_code=400,detail='Ảnh không hợp lệ hoặc vượt quá dung lượng cho phép.')
                row = None
                if category_id:
                    row=con.execute('UPDATE categories SET icon=? WHERE id=? RETURNING id',(image_data,category_id)).fetchone()
                elif category_name:
                    row=con.execute('UPDATE categories SET icon=? WHERE LOWER(name)=LOWER(?) RETURNING id',(image_data,category_name)).fetchone()
                    if not row:
                        row=con.execute('INSERT INTO categories(name,created_by,created_at,icon) VALUES(?,?,?,?) RETURNING id',(category_name,admin['id'],now(),image_data)).fetchone()
                if not row: raise HTTPException(status_code=404,detail='Không tìm thấy danh mục cần cập nhật ảnh.')
                return {'success':True,'message':'Đã lưu ảnh danh mục. Ảnh sẽ hiển thị cho tất cả người dùng.'}
            if action=='stats':
                return {'stats':con.execute("SELECT (SELECT COUNT(*) FROM users) AS users,(SELECT COALESCE(SUM(amount),0) FROM deposits WHERE status='approved') AS deposits,(SELECT COALESCE(SUM(balance),0) FROM users) AS balances,(SELECT COUNT(*) FROM purchases) AS purchases").fetchone()}
            if action=='create_coupon':
                percent=int(data.get('discount_percent') or 0); uses=int(data.get('max_uses') or 0)
                if percent < 1 or percent > 90 or uses < 1 or uses > 1000000:
                    raise HTTPException(status_code=400,detail='Phần trăm giảm phải từ 1–90 và lượt dùng phải lớn hơn 0.')
                code=None
                for _ in range(30):
                    candidate=f"{secrets.randbelow(100000):05d}"
                    if not con.execute('SELECT 1 FROM discount_coupons WHERE code=?',(candidate,)).fetchone():
                        code=candidate; break
                if code is None: raise HTTPException(status_code=503,detail='Không tạo được mã duy nhất, vui lòng thử lại.')
                con.execute('INSERT INTO discount_coupons(code,discount_percent,max_uses,created_by,created_at) VALUES(?,?,?,?,?)',(code,percent,uses,admin['id'],now()))
                return {'success':True,'code':code,'message':f'Đã tạo mã {code} giảm {percent}% · {uses} lượt.'}
            if action=='seller_stats':
                uid=int(data.get('user_id') or 0)
                seller=con.execute('SELECT user_id,username,full_name,is_seller FROM users WHERE user_id=?',(uid,)).fetchone()
                if not seller: raise HTTPException(status_code=404,detail='Không tìm thấy người dùng.')
                deposits=con.execute("SELECT COUNT(*) AS n,COALESCE(SUM(amount),0) AS total FROM deposits WHERE user_id=? AND status='approved'",(uid,)).fetchone()
                buys=con.execute('SELECT COUNT(*) AS n,COALESCE(SUM(quantity),0) AS keys,COALESCE(SUM(total_price),0) AS total FROM purchases WHERE user_id=?',(uid,)).fetchone()
                return {'seller':seller,'deposits':deposits,'purchases':buys}
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
                for dur,label in [('1h','1 giờ'),('1d','1 ngày')]: con.execute('INSERT INTO product_options(product_id,duration,duration_label,price,sell_price) VALUES(?,?,?,?,?)',(pid,dur,label,0,0))
                return {'success':True,'message':f'Đã tạo sản phẩm {name} cùng các gói thời hạn mặc định.'}
            if action=='add_guide':
                cid=int(data.get('category_id')); url=str(data.get('url','')).strip()
                if not url.startswith(('https://','http://')): raise HTTPException(status_code=400,detail='Link phải bắt đầu bằng http:// hoặc https://')
                con.execute('UPDATE categories SET guide_url=? WHERE id=?',(url,cid)); return {'success':True,'message':'Đã lưu link hướng dẫn.'}
            if action=='map_shop':
                oid=int(data.get('option_id')); sid=str(data.get('shopyuri_product_id','')).strip()
                if not sid.isdigit(): raise HTTPException(status_code=400,detail='Shopyuri product ID phải là số.')
                con.execute('UPDATE product_options SET shopyuri_product_id=? WHERE id=?',(sid,oid)); return {'success':True,'message':'Đã gán ID Shopyuri.'}
            if action in ('edit_price','edit_sell_price'):
                oid=int(data.get('option_id')); price=int(data.get('price'))
                if price<0: raise HTTPException(status_code=400,detail='Giá không được âm.')
                col='price' if action=='edit_price' else 'sell_price'; con.execute(f'UPDATE product_options SET {col}=? WHERE id=?',(price,oid)); return {'success':True,'message':'Đã cập nhật giá.'}
            if action=='add_duration':
                pid=int(data.get('product_id')); label=str(data.get('duration_label','')).strip()
                if not label or len(label)>40 or not re.search(r'\d',label): raise HTTPException(status_code=400,detail='Thời hạn không hợp lệ. Ví dụ: 12 giờ hoặc 2 ngày.')
                product=con.execute('SELECT id FROM products WHERE id=?',(pid,)).fetchone()
                if not product: raise HTTPException(status_code=404,detail='Không tìm thấy sản phẩm.')
                if con.execute('SELECT 1 FROM product_options WHERE product_id=? AND LOWER(duration_label)=LOWER(?)',(pid,label)).fetchone():
                    raise HTTPException(status_code=409,detail='Sản phẩm đã có thời hạn này.')
                code=f"custom_{int(time.time()*1000)}"
                con.execute('INSERT INTO product_options(product_id,duration,duration_label,price,sell_price) VALUES(?,?,?,0,0)',(pid,code,label))
                return {'success':True,'message':f'Đã thêm thời hạn {label}. Đã đồng bộ lên Mini App với giá mặc định 0đ.'}
            if action=='rename_duration':
                oid=int(data.get('option_id')); label=str(data.get('duration_label','')).strip()
                if not label or len(label)>40 or not re.search(r'\d',label): raise HTTPException(status_code=400,detail='Thời hạn không hợp lệ. Ví dụ: 1 ngày hoặc 12 giờ.')
                row=con.execute('SELECT id,product_id FROM product_options WHERE id=?',(oid,)).fetchone()
                if not row: raise HTTPException(status_code=404,detail='Không tìm thấy gói thời hạn.')
                if con.execute('SELECT 1 FROM product_options WHERE product_id=? AND id<>? AND LOWER(duration_label)=LOWER(?)',(row['product_id'],oid,label)).fetchone():
                    raise HTTPException(status_code=409,detail='Sản phẩm đã có thời hạn này.')
                normalized=label.lower().replace(' ','')
                known={'1gio':'1h','1giờ':'1h','12gio':'12h','12giờ':'12h','1ngay':'1d','1ngày':'1d','7ngay':'7d','7ngày':'7d','30ngay':'30d','30ngày':'30d'}
                code=known.get(normalized,f"custom_{int(time.time()*1000)}")
                if con.execute('SELECT 1 FROM product_options WHERE product_id=? AND id<>? AND duration=?',(row['product_id'],oid,code)).fetchone(): code=f"custom_{int(time.time()*1000)}"
                con.execute('UPDATE product_options SET duration=?,duration_label=? WHERE id=?',(code,label,oid))
                return {'success':True,'message':f'Đã đổi thời hạn thành {label}. Mini App đã được đồng bộ.'}
            if action=='delete_duration':
                oid=int(data.get('option_id'))
                row=con.execute('SELECT id,duration_label FROM product_options WHERE id=?',(oid,)).fetchone()
                if not row: raise HTTPException(status_code=404,detail='Không tìm thấy thời hạn.')
                con.execute('DELETE FROM product_options WHERE id=?',(oid,))
                return {'success':True,'message':f"Đã xóa thời hạn {row['duration_label']} và key gắn với gói đó. Lịch sử đơn mua vẫn được giữ."}
            if action=='rename_category':
                cid=int(data.get('category_id')); name=str(data.get('name','')).strip()
                if not name: raise HTTPException(status_code=400,detail='Tên mới không được trống.')
                con.execute('UPDATE categories SET name=? WHERE id=?',(name,cid)); return {'success':True,'message':'Đã đổi tên danh mục.'}
            if action=='toggle_product_sale':
                pid=int(data.get('product_id') or 0)
                row=con.execute('SELECT id,name,is_active FROM products WHERE id=? FOR UPDATE',(pid,)).fetchone()
                if not row: raise HTTPException(status_code=404,detail='Không tìm thấy sản phẩm.')
                new_state=not bool(row['is_active'])
                con.execute('UPDATE products SET is_active=? WHERE id=?',(new_state,pid))
                return {'success':True,'is_active':new_state,'message':('Đã bật bán lại sản phẩm ' if new_state else 'Đã dừng bán sản phẩm ')+row['name']+'. Dữ liệu, key và lịch sử vẫn được giữ nguyên.'}
            if action=='delete_product':
                ident=int(data.get('id') or data.get('product_id') or 0)
                row=con.execute('SELECT name FROM products WHERE id=? FOR UPDATE',(ident,)).fetchone()
                if not row: raise HTTPException(status_code=404,detail='Không tìm thấy sản phẩm.')
                con.execute('UPDATE products SET is_active=FALSE WHERE id=?',(ident,))
                return {'success':True,'message':'Đã dừng bán sản phẩm '+row['name']+'. Dữ liệu, key và lịch sử vẫn được giữ nguyên.'}
            if action=='delete_category':
                ident=int(data.get('id')); cur=con.execute('DELETE FROM categories WHERE id=?',(ident,))
                if not cur.rowcount: raise HTTPException(status_code=404,detail='Không tìm thấy danh mục cần xóa.')
                return {'success':True,'message':'Đã xóa danh mục.'}
            if action=='add_keys':
                oid=int(data.get('option_id')); keys=clean_lines(str(data.get('keys','')))
                if not keys: raise HTTPException(status_code=400,detail='Không có key hợp lệ.')
                for key in keys:
                    cur=con.execute("INSERT INTO product_keys(option_id,key_value,added_by,added_at) VALUES(?,?,?,?) ON CONFLICT(key_value) DO NOTHING",(oid,key,admin['id'],now())); added_keys+=cur.rowcount
                stock=con.execute("SELECT COUNT(*) AS n FROM product_keys WHERE option_id=? AND status='available'",(oid,)).fetchone()['n']
                stock_info=con.execute('''SELECT c.name AS category_name,p.name AS product_name,po.duration_label
                    FROM product_options po JOIN products p ON p.id=po.product_id
                    JOIN categories c ON c.id=p.category_id WHERE po.id=?''',(oid,)).fetchone()
                add_keys_result={'success':True,'message':f'Đã thêm {added_keys}/{len(keys)} key. Kho hiện có {stock} key.'}
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
        if action=='add_keys':
            if added_keys and stock_info:
                announcement=(f"🔥 CÓ HÀNG MỚI VỀ! 🔥\n━━━━━━━━━━━━━━━━━━\n"
                    f"📁 Danh mục: {stock_info['category_name']}\n"
                    f"🎮 Sản phẩm: {stock_info['duration_label']}\n"
                    f"🔢 Số lượng: {added_keys} sản phẩm đã được cập nhật!\n\n"
                    f"👉 Nhấn nút bên dưới để mở Mini App và mua ngay!")
                try:
                    sent,total=await broadcast_new_stock_http(announcement)
                    add_keys_result['message']+=f' Đã thông báo tới {sent}/{total} người dùng.'
                except Exception:
                    log.exception('Could not send the Mini App stock announcement')
                    add_keys_result['message']+=' Chưa gửi được thông báo; key đã được lưu thành công.'
            return add_keys_result
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
    app = Application.builder().token(BOT_TOKEN).post_init(configure_miniapp_menu_button).build()
    telegram_application = app
    app.add_handler(CommandHandler('start', start)); app.add_handler(CommandHandler('admin', admin_cmd))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.Sticker.ALL | filters.VIDEO | filters.ANIMATION | filters.Document.ALL | filters.AUDIO | filters.VOICE | filters.FORWARDED) & ~filters.COMMAND,
        text_handler,
    ))
    app.add_error_handler(error_handler)
    port = int(os.getenv('PORT', '3000'))
    web_thread = threading.Thread(target=lambda: uvicorn.run(web_app, host='0.0.0.0', port=port, log_level='info'), daemon=True)
    web_thread.start()
    log.info('Shop bot + Mini App API started on port %s', port)
    app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)

if __name__ == '__main__': main()
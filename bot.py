import os
import re
import asyncio
import logging
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from html import escape
from pathlib import Path
from urllib.parse import urlencode
from dotenv import load_dotenv
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env')

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, MessageEntity
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
QR_MEMO_PREFIX = 'SEVQR'  # VietinBank requires SEVQR in the transfer description.
QR_MEMO_SUFFIX = os.getenv('QR_MEMO_SUFFIX', 'NAP').strip().upper() or 'NAP'
DB_POOL = None
logging.basicConfig(format='%(asctime)s | %(levelname)s | %(name)s | %(message)s', level=LOG_LEVEL)
log = logging.getLogger('key_shop_bot')

USER_KB = ReplyKeyboardMarkup([[KeyboardButton('↩️ Trở về')]], resize_keyboard=True, is_persistent=True)

ADMIN_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton('📢 Thông báo', callback_data='adm:broadcast')],
    [InlineKeyboardButton('👤 Thông tin người dùng', callback_data='adm:usercheck')],
    [InlineKeyboardButton('➕ Thêm sản phẩm', callback_data='adm:addproduct')],
    [InlineKeyboardButton('🔗 Gán ID ShopHenry cho gói sản phẩm', callback_data='adm:mapshop')],
    [InlineKeyboardButton('✏️ Sửa giá / thời hạn', callback_data='adm:editproduct')],
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

def init_db():
    global DB_POOL
    if DB_POOL is None:
        DB_POOL = ConnectionPool(
            conninfo=DATABASE_URL, min_size=1, max_size=10, timeout=15,
            kwargs={'row_factory': dict_row}, open=True
        )
        DB_POOL.wait(timeout=15)
    statements = [
        """CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY, username TEXT, full_name TEXT NOT NULL DEFAULT '',
            balance BIGINT NOT NULL DEFAULT 0, is_locked BOOLEAN NOT NULL DEFAULT FALSE,
            joined_at TEXT NOT NULL, last_seen TEXT NOT NULL)""",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_deposit_request_at TIMESTAMPTZ",
        "ALTER TABLE product_options ADD COLUMN IF NOT EXISTS shophenry_product_id TEXT",
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
        """INSERT INTO product_options(product_id,duration,duration_label,price)
            SELECT p.id, d.duration, d.label, 0
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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('👤 Tài Khoản', callback_data='menu:account'), InlineKeyboardButton('📜 Lịch Sử Mua', callback_data='menu:history')],
        [InlineKeyboardButton('🛍 Cửa Hàng', callback_data='shop:home'), InlineKeyboardButton('💳 Nạp Tiền', callback_data='menu:deposit')],
        [InlineKeyboardButton('🆘 Hỗ Trợ', callback_data='menu:support')],
    ])

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
    with db() as con: return con.execute('SELECT id,name FROM categories ORDER BY id').fetchall()
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
        await message.reply_text(f'🆔 ID của bạn: <code>{user.id}</code>', parse_mode='HTML', reply_markup=USER_KB)
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

# Bấm vào danh mục ngoài -> Hiển thị thẳng các gói thời hạn của tất cả sản phẩm trong danh mục đó luôn
async def show_category(q, category_id):
    with db() as con:
        cat = con.execute('SELECT * FROM categories WHERE id=?', (category_id,)).fetchone()
        prods = con.execute('SELECT id, name FROM products WHERE category_id::text=?::text', (category_id,)).fetchall()
        prod_ids = [p['id'] for p in prods]
        opts = []
        if prod_ids:
            placeholders = ','.join(['?'] * len(prod_ids))
            opts = con.execute(f"SELECT po.*, p.name AS product_name FROM product_options po JOIN products p ON p.id::text=po.product_id::text WHERE po.product_id::text IN ({placeholders}) ORDER BY po.id", [str(pid) for pid in prod_ids]).fetchall()
            
    if not cat: await q.edit_message_text('Danh mục không còn tồn tại.'); return
    rows = []
    for o in opts:
        stock_label = 'ShopHenry' if o.get('shophenry_product_id') else f"{stock_for_option(o['id'])}"
        btn_text = f"✨ {o['product_name']} ({o['duration_label']}) – {o['price']:,}đ (Nguồn: {stock_label})"
        rows.append([InlineKeyboardButton(btn_text, callback_data=f"shop:option:{o['id']}")])
    rows.append([InlineKeyboardButton('🌐 Quay Lại', callback_data='shop:home')])
    
    txt = f"✨ {escape(cat['name'])}\n\n❝ Chọn gói sản phẩm bên dưới để xem chi tiết và mua: ❞"
    await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(rows), parse_mode='HTML')

async def show_quantity(q, option_id, context):
    with db() as con:
        o=con.execute('SELECT po.*,p.name AS product_name,p.category_id,c.name AS category_name FROM product_options po JOIN products p ON p.id::text=po.product_id::text JOIN categories c ON c.id::text=p.category_id::text WHERE po.id=?',(option_id,)).fetchone()
    if not o: await q.edit_message_text('Gói sản phẩm không còn tồn tại.'); return
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
    await q.edit_message_text(
        f"🛒 {escape(o['product_name'])}\n⏳ {escape(o['duration_label'])}"
        f"\n💵 Giá: {o['price']:,}đ / key\n📦 Nguồn hàng: {'ShopHenry' if o.get('shophenry_product_id') else f'Kho nội bộ ({stock_for_option(option_id)})'}"
        f"\n\n🔑 Chọn số lượng Key muốn mua để tiến hành thanh toán:",
        reply_markup=InlineKeyboardMarkup(buttons),parse_mode='HTML')

async def show_confirm(q, option_id, qty, context):
    with db() as con:
        o=con.execute('SELECT po.*,p.name AS product_name,p.category_id,c.name AS category_name FROM product_options po JOIN products p ON p.id::text=po.product_id::text JOIN categories c ON c.id::text=p.category_id::text WHERE po.id=?',(option_id,)).fetchone()
    if not o: await q.edit_message_text('Sản phẩm không còn tồn tại.'); return
    total=o['price']*qty
    context.user_data.update(pending_purchase={'option_id':option_id,'quantity':qty,'total':total})
    await q.edit_message_text(f"🧾 XÁC NHẬN MUA HÀNG\n\n📁 Danh mục: {escape(o['category_name'])}\n📦 Sản phẩm: {escape(o['product_name'])}\n⏳ Thời hạn: {escape(o['duration_label'])}\n🔢 Số lượng: {qty}\n💰 Tổng tiền: {total:,}đ\n\nBạn xác nhận mua chứ?",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('✅ XÁC NHẬN MUA',callback_data='shop:confirm')],[InlineKeyboardButton('❌ HỦY',callback_data='shop:cancel')]]),parse_mode='HTML')

def shophenry_buy(product_id, quantity):
    """Call ShopHenry once. Never auto-retry a purchase because it may charge twice."""
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
    pending=context.user_data.get('pending_purchase')
    if not pending:
        await q.edit_message_text('Phiên mua hàng đã hết. Hãy mở cửa hàng lại.')
        return
    uid=update.effective_user.id; oid=pending['option_id']; qty=pending['quantity']
    con=db()
    try:
        con.execute('BEGIN')
        user=con.execute('SELECT balance,is_locked FROM users WHERE user_id=? FOR UPDATE',(uid,)).fetchone()
        opt=con.execute('''SELECT po.*,p.name AS product_name,p.category_id,
            c.name AS category_name FROM product_options po
            JOIN products p ON p.id::text=po.product_id::text
            JOIN categories c ON c.id::text=p.category_id::text WHERE po.id=?''',(oid,)).fetchone()
        if not user or user['is_locked']:
            con.rollback(); await q.edit_message_text('⛔ Tài khoản không thể mua hàng.'); return
        if not opt:
            con.rollback(); await q.edit_message_text('Sản phẩm không còn tồn tại.'); return
        total=opt['price']*qty
        if user['balance']<total:
            con.rollback(); await q.edit_message_text(f"❌ Số dư không đủ.\nCần: {total:,}đ\nHiện có: {user['balance']:,}đ"); return
        supplier_order=''
        if opt['shophenry_product_id']:
            # Call supplier while this user's balance row is locked, preventing parallel double purchases.
            try:
                key_text, supplier_order = await asyncio.to_thread(
                    shophenry_buy, opt['shophenry_product_id'], qty
                )
                keys=[]
            except Exception as api_error:
                con.rollback()
                log.warning('ShopHenry purchase failed for user=%s product=%s: %s', uid, opt['shophenry_product_id'], api_error)
                await q.edit_message_text('❌ Không thể hoàn tất đơn ShopHenry.\n' + str(api_error)[:700] + '\n\nBot chưa trừ số dư của bạn. Nếu ShopHenry đã tạo đơn, admin cần kiểm tra mã đơn trước khi thử lại.')
                return
        else:
            # Preserve the bot's original local-key inventory for products not connected to ShopHenry.
            keys=con.execute("SELECT id,key_value FROM product_keys WHERE option_id=? AND status='available' ORDER BY id LIMIT ? FOR UPDATE SKIP LOCKED",(oid,qty)).fetchall()
            if len(keys)<qty:
                con.rollback(); await q.edit_message_text(f"❌ Kho không đủ key. Hiện còn {len(keys)} key."); return
            key_text='\n'.join(k['key_value'] for k in keys)
        stamp=now()
        con.execute('UPDATE users SET balance=balance-? WHERE user_id=?',(total,uid))
        for k in keys:
            con.execute("UPDATE product_keys SET status='sold',sold_to=?,sold_at=? WHERE id=?",(uid,stamp,k['id']))
        con.execute('INSERT INTO purchases(user_id,category_name,product_name,duration_label,quantity,unit_price,total_price,keys_text,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                    (uid,opt['category_name'],opt['product_name'],opt['duration_label'],qty,opt['price'],total,key_text,stamp))
        con.commit()
    except Exception:
        con.rollback(); log.exception('Purchase failed after ShopHenry request')
        await q.message.reply_text('⚠️ Có lỗi khi ghi nhận đơn sau khi gọi ShopHenry. Không bấm mua lại; hãy gửi mã đơn cho admin kiểm tra.')
        return
    finally:
        con.close()
    context.user_data.pop('pending_purchase',None)
    await q.edit_message_text(
        f"🎉 MUA HÀNG THÀNH CÔNG! 🎉\n\n📦 Sản phẩm: {escape(opt['product_name'])} - {escape(opt['duration_label'])}\n🔢 Số lượng: {qty}\n💰 Tổng tiền: {total:,}đ"
        + (f"\n🧾 Mã đơn ShopHenry: {escape(supplier_order)}" if supplier_order else ''),
        parse_mode='HTML', reply_markup=inline_back('menu:home'))
    await q.message.reply_text('🔑 KEY CỦA BẠN:', reply_markup=USER_KB)
    key_lines=key_text.splitlines()
    chunk=[]; chunk_len=0
    for line in key_lines:
        extra=len(line)+1
        if chunk and chunk_len+extra>3400:
            await q.message.reply_text('<pre>'+escape('\n'.join(chunk))+'</pre>',parse_mode='HTML',reply_markup=USER_KB)
            chunk=[]; chunk_len=0
        chunk.append(line); chunk_len+=extra
    if chunk:
        await q.message.reply_text('<pre>'+escape('\n'.join(chunk))+'</pre>',parse_mode='HTML',reply_markup=USER_KB)

async def user_history(q, uid):
    with db() as con: rows=con.execute('SELECT * FROM purchases WHERE user_id=? ORDER BY id DESC LIMIT 20',(uid,)).fetchall()
    if not rows:
        await q.edit_message_text('📜 Bạn chưa có lịch sử mua hàng.', reply_markup=inline_back('menu:home'))
        return
    out=['📜 LỊCH SỬ MUA HÀNG']
    for r in rows: out.append(f"\n🧾 Đơn #{r['id']} · {r['created_at']}\n{r['product_name']} · {r['duration_label']}\nSố lượng: {r['quantity']} · {r['total_price']:,}đ")
    await q.edit_message_text('\n'.join(out), reply_markup=inline_back('menu:home'))

async def broadcast(bot, text):
    with db() as con: ids=[r['user_id'] for r in con.execute('SELECT user_id FROM users WHERE is_locked=FALSE').fetchall()]
    sent=0
    for uid in ids:
        try: await bot.send_message(chat_id=uid,text=text); sent+=1; await asyncio.sleep(0.04)
        except Exception as e: log.info('Broadcast skipped user %s: %s',uid,e)
    return sent

async def show_news_emoji_page(target, context, page=0):
    """Render a page of custom emojis from the NewsEmoji Telegram set."""
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
    q=update.callback_query; await q.answer(); data=q.data or ''; user=update.effective_user
    if user and not is_admin(update):
        if maintenance_enabled():
            await q.edit_message_text('🛠 Bot đang bảo trì. Vui lòng quay lại sau.', reply_markup=inline_back('menu:home')); return
        with db() as con:
            status=con.execute('SELECT is_locked FROM users WHERE user_id=?',(user.id,)).fetchone()
        if status and status['is_locked']:
            await q.edit_message_text('⛔ Tài khoản của bạn đang bị khóa.'); return
    if data.startswith('deposit:approve:') or data.startswith('deposit:reject:'):
        if not is_admin(update):
            await q.edit_message_text('⛔ Bạn không có quyền duyệt đơn.'); return
        await handle_deposit_review(q, context, data); return
    if data.startswith('adm:') or data.startswith('admin:') or data.startswith('user:'):
        if not is_admin(update): await q.edit_message_text('⛔ Không có quyền truy cập.'); return
    
    if data.startswith('deposit:submitted:'):
        deposit_id=int(data.split(':')[2])
        with db() as con:
            dep=con.execute('SELECT d.*,u.full_name,u.username FROM deposits d JOIN users u ON u.user_id=d.user_id WHERE d.id=? AND d.user_id=? FOR UPDATE',(deposit_id,user.id)).fetchone()
            if not dep:
                await q.edit_message_caption(caption='Không tìm thấy đơn nạp.',reply_markup=None); return
            if dep['status']!='pending':
                await q.edit_message_caption(caption='Đơn này đã được gửi hoặc xử lý.',reply_markup=None); return
            con.execute("UPDATE deposits SET status='submitted' WHERE id=?",(deposit_id,))
        await q.edit_message_caption(caption=f"📨 Đã gửi yêu cầu xác nhận nạp #{deposit_id}. Admin sẽ kiểm tra giao dịch.\n💰 Số tiền: {dep['amount']:,}đ\n📝 Nội dung: {dep['note']}",reply_markup=None)
        await notify_admins(context,deposit_id,dep['amount'],dep['note'],dep['full_name'],user.id,dep['username'])
    elif data.startswith('deposit:cancel:'):
        deposit_id=int(data.split(':')[2])
        with db() as con:
            con.execute("UPDATE deposits SET status='cancelled' WHERE id=? AND user_id=? AND status='pending'",(deposit_id,user.id))
        await q.edit_message_caption(caption='❌ Đơn nạp tiền đã được hủy.',reply_markup=None)
    elif data == 'menu:home':
        context.user_data.pop('flow', None)
        with db() as con:
            row = con.execute('SELECT balance,is_locked FROM users WHERE user_id=?', (user.id,)).fetchone()
        if not row:
            await q.edit_message_text('Bạn chưa có tài khoản. Hãy gửi /start để bắt đầu.')
        elif row['is_locked']:
            await q.edit_message_text('⛔ Tài khoản của bạn đang bị khóa.')
        else:
            main_menu_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton('👤 Tài Khoản', callback_data='menu:account'),
                 InlineKeyboardButton('📜 Lịch Sử Mua', callback_data='menu:history')],
                [InlineKeyboardButton('🛍 Cửa Hàng', callback_data='shop:home'),
                 InlineKeyboardButton('💳 Nạp Tiền', callback_data='menu:deposit')],
                [InlineKeyboardButton('🆘 Hỗ Trợ', callback_data='menu:support')],
            ])
            await q.edit_message_text(user_menu_text(user, row['balance']), reply_markup=main_menu_kb, parse_mode='HTML')
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
        await q.edit_message_text('💳 NẠP TIỀN\n\nNhập số tiền bạn muốn nạp (VNĐ):', reply_markup=inline_back('menu:home'))
    elif data == 'menu:support':
        await q.edit_message_text('🆘 HỖ TRỢ\nVui lòng liên hệ quản trị viên.', reply_markup=inline_back('menu:home'))
    elif data=='shop:home': await show_shop(q.message,edit=True)
    elif data.startswith('shop:cat:'): await show_category(q,int(data.split(':')[2]))
    elif data.startswith('shop:option:'): await show_quantity(q,int(data.split(':')[2]),context)
    elif data.startswith('shop:qty:'):
        _,_,oid,qty=data.split(':')
        if qty=='other': context.user_data['flow']='custom_quantity'; context.user_data['selected_option']=int(oid); await q.message.reply_text('Nhập số lượng key muốn mua (1–1.000):',reply_markup=USER_KB)
        else: await show_confirm(q,int(oid),int(qty),context)
    elif data=='shop:confirm': await do_purchase(q,update,context)
    elif data=='shop:cancel': context.user_data.pop('pending_purchase',None); await q.edit_message_text('Đã hủy đơn mua hàng.',reply_markup=inline_back('shop:home'))
    elif data=='adm:menu': await q.edit_message_text(admin_menu_text(),reply_markup=ADMIN_KB)
    elif data=='adm:emoji':
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
    elif data=='adm:maintenance':
        with db() as con:
            current=con.execute("SELECT setting_value FROM bot_settings WHERE setting_key='maintenance'").fetchone()
            new_value='off' if current and current['setting_value']=='on' else 'on'
            con.execute("INSERT INTO bot_settings(setting_key,setting_value,updated_at) VALUES('maintenance',?,?) ON CONFLICT(setting_key) DO UPDATE SET setting_value=EXCLUDED.setting_value,updated_at=EXCLUDED.updated_at",(new_value,now()))
        label='ĐANG BẬT' if new_value=='on' else 'ĐANG TẮT'
        await q.edit_message_text(f'🛠 CHẾ ĐỘ BẢO TRÌ: {label}\n'+('Người dùng thường không thể sử dụng bot.' if new_value=='on' else 'Người dùng có thể sử dụng bot bình thường.'),reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔄 Bật / Tắt bảo trì',callback_data='adm:maintenance')],[InlineKeyboardButton('↩️ Quay lại Admin',callback_data='adm:menu')]]))
    elif data=='adm:broadcast': context.user_data['flow']='broadcast'; await q.message.reply_text('Gửi nội dung thông báo muốn gửi đến tất cả người dùng:')
    elif data=='adm:usercheck': context.user_data['flow']='user_check_id'; await q.message.reply_text('Nhập Telegram ID hoặc @username cần kiểm tra:')
    elif data=='adm:addproduct': await q.edit_message_text('Chọn loại sản phẩm muốn thêm:',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('📁 Sản phẩm ngoài (danh mục)',callback_data='adm:addcat')],[InlineKeyboardButton('📦 Sản phẩm trong (sản phẩm)',callback_data='adm:addinner')],[InlineKeyboardButton('↩️ Quay lại',callback_data='adm:menu')]]))
    elif data=='adm:addcat': context.user_data['flow']='add_category_name'; await q.message.reply_text('Nhập tên danh mục ngoài:')
    elif data=='adm:mapshop':
        cats=get_categories()
        if not cats:
            await q.message.reply_text('Chưa có danh mục sản phẩm.'); return
        await q.edit_message_text('Chọn danh mục chứa sản phẩm cần gán ID ShopHenry:',reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(c['name'],callback_data='adm:mapshopcat:{}'.format(c['id']))] for c in cats]+[[InlineKeyboardButton('↩️ Quay lại',callback_data='adm:menu')]]))
    elif data.startswith('adm:mapshopcat:'):
        cid=int(data.split(':')[2]); ps=get_products(cid)
        await q.edit_message_text('Chọn sản phẩm cần gán ID ShopHenry:',reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(p['name'],callback_data='adm:mapshopprod:{}'.format(p['id']))] for p in ps]+[[InlineKeyboardButton('↩️ Quay lại',callback_data='adm:mapshop')]]))
    elif data.startswith('adm:mapshopprod:'):
        pid=int(data.split(':')[2])
        with db() as con: options=con.execute('SELECT id,duration_label,shophenry_product_id FROM product_options WHERE product_id=? ORDER BY id',(pid,)).fetchall()
        await q.edit_message_text('Chọn gói thời hạn cần gán ID ShopHenry:',reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"{o['duration_label']} · ID: {o['shophenry_product_id'] or 'Chưa gán'}",callback_data=f"adm:mapshopopt:{o['id']}")] for o in options]+[[InlineKeyboardButton('↩️ Quay lại',callback_data=f'adm:mapshopcat:{get_product_category_id(pid)}')]]))
    elif data.startswith('adm:mapshopopt:'):
        oid=int(data.split(':')[2])
        context.user_data.update(flow='map_shophenry_id',shophenry_target_option=oid)
        await q.message.reply_text('Nhập product_id của đúng gói thời hạn này trên ShopHenry (chỉ nhập ID dạng số):')
    elif data=='adm:addinner':
        cats=get_categories()
        if not cats: await q.message.reply_text('Chưa có danh mục ngoài. Hãy thêm danh mục trước.'); return
        await q.edit_message_text('Chọn danh mục ngoài chứa sản phẩm mới:',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(c['name'],callback_data=f"adm:innercat:{c['id']}")] for c in cats]+[[InlineKeyboardButton('↩️ Quay lại',callback_data='adm:menu')]]))
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
            await q.message.reply_text('⚠️ Không thể chọn danh mục. Vui lòng thử lại hoặc kiểm tra log Paella.')
        return
    elif data=='adm:renamecat':
        cats=get_categories(); await q.edit_message_text('Chọn danh mục ngoài muốn đổi tên:',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(c['name'],callback_data=f"adm:renamecatid:{c['id']}")] for c in cats]+[[InlineKeyboardButton('↩️ Quay lại',callback_data='adm:menu')]]))
    elif data.startswith('adm:renamecatid:'):
        context.user_data.update(flow='rename_category',rename_category_id=int(data.split(':')[2]))
        await q.message.reply_text('Nhập tên mới cho danh mục ngoài:')
    elif data=='adm:detailproduct':
        cats=get_categories()
        await q.edit_message_text('📋 CHI TIẾT SẢN PHẨM\nChọn danh mục:',reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(c['name'],callback_data=f"adm:detailcat:{c['id']}")] for c in cats]+
            [[InlineKeyboardButton('↩️ Quay lại',callback_data='adm:menu')]]))
    elif data.startswith('adm:detailcat:'):
        cid=int(data.split(':')[2]); ps=get_products(cid)
        await q.edit_message_text('Chọn sản phẩm cần xem/sửa:',reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(p['name'],callback_data=f"adm:detailprod:{p['id']}")] for p in ps]+
            [[InlineKeyboardButton('↩️ Quay lại',callback_data='adm:detailproduct')]]))
    elif data.startswith('adm:detailprod:'):
        pid=int(data.split(':')[2])
        with db() as con:
            opts=con.execute('SELECT * FROM product_options WHERE product_id::text=?::text ORDER BY id',(pid,)).fetchall()
        rows=[[InlineKeyboardButton(f"{o['duration_label']} · {o['price']:,}đ",callback_data=f"adm:detailoption:{o['id']}")] for o in opts]
        rows.append([InlineKeyboardButton('↩️ Quay lại',callback_data='adm:detailproduct')])
        await q.edit_message_text('📋 CHI TIẾT SẢN PHẨM\nChọn gói muốn thay đổi giá và thời hạn:',reply_markup=InlineKeyboardMarkup(rows))
    elif data.startswith('adm:detailoption:'):
        oid=int(data.split(':')[2])
        context.user_data.update(flow='edit_option_price',edit_option_id=oid)
        await q.message.reply_text('Nhập giá mới bằng số VNĐ (ví dụ 50000):')
    elif data=='adm:editproduct':
        cats=get_categories(); await q.edit_message_text('Chọn danh mục chứa sản phẩm cần sửa:',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(c['name'],callback_data=f"adm:editcat:{c['id']}")] for c in cats]+[[InlineKeyboardButton('↩️ Quay lại',callback_data='adm:menu')]]))
    elif data.startswith('adm:editcat:'):
        cid=int(data.split(':')[2]); ps=get_products(cid); await q.edit_message_text('Chọn sản phẩm cần sửa:',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(p['name'],callback_data=f"adm:editprod:{p['id']}")] for p in ps]+[[InlineKeyboardButton('↩️ Quay lại',callback_data='adm:menu')]]))
    elif data.startswith('adm:editprod:'):
        pid=int(data.split(':')[2])
        with db() as con: opts=con.execute('SELECT * FROM product_options WHERE product_id::text=?::text ORDER BY duration',(pid,)).fetchall()
        await q.edit_message_text('Chọn gói thời hạn cần sửa giá/tên thời hạn:',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{o['duration_label']} · {o['price']:,}đ",callback_data=f"adm:editoption:{o['id']}")] for o in opts]+[[InlineKeyboardButton('↩️ Quay lại',callback_data='adm:menu')]]))
    elif data.startswith('adm:editoption:'):
        oid=int(data.split(':')[2]); context.user_data.update(flow='edit_option_price',edit_option_id=oid)
        await q.message.reply_text('Nhập giá mới bằng số VNĐ (ví dụ 50000):')
    elif data=='adm:delproduct':
        await q.edit_message_text('Chọn phần cần xóa:',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('📁 Xóa danh mục ngoài',callback_data='adm:delcat')],[InlineKeyboardButton('📦 Xóa sản phẩm trong',callback_data='adm:delinner')],[InlineKeyboardButton('↩️ Quay lại',callback_data='adm:menu')]]))
    elif data=='adm:delcat':
        cats=get_categories(); await q.edit_message_text('Chọn danh mục ngoài cần xóa:',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(c['name'],callback_data=f"adm:delcatconfirm:{c['id']}")] for c in cats]+[[InlineKeyboardButton('↩️ Quay lại',callback_data='adm:menu')]]))
    elif data.startswith('adm:delcatconfirm:'):
        cid=int(data.split(':')[2]); context.user_data['delete_target']=('category',cid); await q.edit_message_text('Xác nhận xóa danh mục và toàn bộ sản phẩm/key bên trong?',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⚠️ XÁC NHẬN XÓA',callback_data='adm:deleteconfirm')],[InlineKeyboardButton('Hủy',callback_data='adm:menu')]]))
    elif data=='adm:delinner':
        cats=get_categories(); await q.edit_message_text('Chọn danh mục chứa sản phẩm cần xóa:',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(c['name'],callback_data=f"adm:delinnercat:{c['id']}")] for c in cats]+[[InlineKeyboardButton('↩️ Quay lại',callback_data='adm:menu')]]))
    elif data.startswith('adm:delinnercat:'):
        cid=int(data.split(':')[2]); ps=get_products(cid); await q.edit_message_text('Chọn sản phẩm cần xóa:',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(p['name'],callback_data=f"adm:delinnerconfirm:{p['id']}")] for p in ps]+[[InlineKeyboardButton('↩️ Quay lại',callback_data='adm:menu')]]))
    elif data.startswith('adm:delinnerconfirm:'):
        pid=int(data.split(':')[2]); context.user_data['delete_target']=('product',pid); await q.edit_message_text('Xác nhận xóa sản phẩm và các gói/key bên trong?',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⚠️ XÁC NHẬN XÓA',callback_data='adm:deleteconfirm')],[InlineKeyboardButton('Hủy',callback_data='adm:menu')]]))
    elif data=='adm:deleteconfirm':
        target=context.user_data.pop('delete_target',None)
        if target:
            with db() as con: con.execute('DELETE FROM categories WHERE id=?',(target[1],)) if target[0]=='category' else con.execute('DELETE FROM products WHERE id=?',(target[1],))
        await q.edit_message_text('✅ Đã xóa thành công.',reply_markup=ADMIN_KB)
    elif data=='adm:addkeys':
        cats=get_categories(); await q.edit_message_text('Chọn danh mục:',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(c['name'],callback_data=f"adm:keycat:{c['id']}")] for c in cats]+[[InlineKeyboardButton('↩️ Quay lại',callback_data='adm:menu')]]))
    elif data.startswith('adm:keycat:'):
        cid=int(data.split(':')[2]); ps=get_products(cid)
        await q.edit_message_text('Chọn sản phẩm bên trong để thêm key:',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(p['name'],callback_data=f"adm:keyproduct:{p['id']}")] for p in ps]+[[InlineKeyboardButton('↩️ Quay lại',callback_data='adm:menu')]]))
    elif data.startswith('adm:keyproduct:'):
        pid=int(data.split(':')[2])
        context.user_data['key_product_id']=pid
        with db() as con:
            options=con.execute('SELECT duration,duration_label FROM product_options WHERE product_id::text=?::text ORDER BY id',(pid,)).fetchall()
        rows=[[InlineKeyboardButton(f"🔑 KEY {o['duration_label']}",callback_data=f"adm:keyduration:{o['duration']}")] for o in options]
        rows.append([InlineKeyboardButton('↩️ Quay lại',callback_data='adm:menu')])
        await q.edit_message_text('Chọn gói thời hạn:',reply_markup=InlineKeyboardMarkup(rows))
    elif data.startswith('adm:keyduration:'):
        dur = data.split(':')[2]
        pid = context.user_data.get('key_product_id')
        if not pid:
            await q.message.reply_text('⚠️ Phiên làm việc đã hết hạn. Vui lòng bấm /admin để thực hiện lại.', reply_markup=ADMIN_KB)
            return
        with db() as con:
            opt = con.execute('SELECT id,duration_label FROM product_options WHERE product_id::text=?::text AND duration=?', (pid, dur)).fetchone()
            if not opt:
                label = DURATIONS[dur]
                cur_ins = con.execute('INSERT INTO product_options(product_id, duration, duration_label, price) VALUES (?, ?, ?, 0) RETURNING id', (pid, dur, label))
                opt_id = cur_ins.fetchone()['id']
                con.commit()
            else:
                opt_id = opt['id']
        context.user_data.update(flow='add_product_keys', key_option_id=opt_id, key_duration=dur)
        label = opt['duration_label'] if opt else DURATIONS[dur]
        await q.message.reply_text(f'Gửi danh sách key {label}, mỗi key một dòng:')
    elif data=='adm:stock':
        cats=get_categories(); await q.edit_message_text('Chọn danh mục để kiểm tra kho:',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(c['name'],callback_data=f"adm:stockcat:{c['id']}")] for c in cats]+[[InlineKeyboardButton('↩️ Quay lại',callback_data='adm:menu')]]))
    elif data.startswith('adm:stockcat:'):
        cid=int(data.split(':')[2]); ps=get_products(cid); await q.edit_message_text('Chọn sản phẩm:',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(p['name'],callback_data=f"adm:stockprod:{p['id']}")] for p in ps]+[[InlineKeyboardButton('↩️ Quay lại',callback_data='adm:menu')]]))
    elif data.startswith('adm:stockprod:'):
        pid=int(data.split(':')[2])
        with db() as con: rows=con.execute("SELECT po.*,COUNT(pk.id) FILTER (WHERE pk.status='available') AS available,COUNT(pk.id) AS total FROM product_options po LEFT JOIN product_keys pk ON pk.option_id=po.id WHERE po.product_id::text=?::text GROUP BY po.id ORDER BY po.duration",(pid,)).fetchall()
        txt='📦 KHO SẢN PHẨM\n'+'\n'.join(f"{r['duration_label']}: còn {r['available']} / tổng {r['total']} · Giá {r['price']:,}đ" for r in rows) if rows else 'Sản phẩm chưa có gói thời hạn.'
        await q.edit_message_text(txt,reply_markup=inline_back('adm:menu'))
    elif data.startswith('user:'):
        await admin_user_callback(q,context,data)

async def admin_user_callback(q,context,data):
    parts=data.split(':'); uid=int(parts[2])
    if parts[1]=='deposit':
        with db() as con: rows=con.execute('SELECT * FROM deposits WHERE user_id=? ORDER BY id DESC LIMIT 20',(uid,)).fetchall()
        body='📥 LỊCH SỬ NẠP TIỀN\n'+'\n'.join(f"#{r['id']} · {r['amount']:,}đ · {r['status']} · {r['note'] or ''} · {r['created_at']}" for r in rows) if rows else 'Chưa có lịch sử nạp tiền.'
        await q.message.reply_text(body)
    elif parts[1]=='purchase':
        with db() as con: rows=con.execute('SELECT * FROM purchases WHERE user_id=? ORDER BY id DESC LIMIT 20',(uid,)).fetchall()
        body='🧾 LỊCH SỬ MUA KEY\n'+'\n'.join(f"#{r['id']} {r['product_name']} · {r['duration_label']} · {r['quantity']} key · {r['total_price']:,}đ" for r in rows) if rows else 'Chưa có lịch sử mua key.'
        await q.message.reply_text(body)
    elif parts[1] in ('lock','unlock'):
        locked = parts[1]=='lock'
        with db() as con:
            con.execute('UPDATE users SET is_locked=? WHERE user_id=?',(locked,uid))
        await q.message.reply_text('🔒 Đã khóa tài khoản.' if locked else '🔓 Đã mở khóa tài khoản.')
        await show_admin_user(q.message,uid)
    elif parts[1] in ('add','subtract'):
        context.user_data.update(flow='admin_balance_amount', balance_action=parts[1], target_user_id=uid)
        verb='cộng vào' if parts[1]=='add' else 'trừ khỏi'
        await q.message.reply_text(f'Nhập số tiền muốn {verb} tài khoản ID {uid} (VNĐ):')

async def show_admin_user(msg, uid):
    with db() as con: r=con.execute('SELECT * FROM users WHERE user_id=?',(uid,)).fetchone()
    if not r:
        await msg.reply_text('Không tìm thấy người dùng.', reply_markup=ADMIN_KB); return
    uname='@'+r['username'] if r['username'] else 'Không có'
    joined=r['joined_at'] or 'Không rõ'; seen=r['last_seen'] or 'Không rõ'
    profile=f"\n🔗 <a href=\"https://t.me/{escape(r['username'])}\">Mở hồ sơ Telegram</a>" if r['username'] else ''
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton('➕ Cộng tiền',callback_data=f'user:add:{uid}'), InlineKeyboardButton('➖ Trừ tiền',callback_data=f'user:subtract:{uid}')],
        [InlineKeyboardButton('📥 Lịch sử nạp',callback_data=f'user:deposit:{uid}'), InlineKeyboardButton('🧾 Lịch sử mua',callback_data=f'user:purchase:{uid}')],
        [InlineKeyboardButton('🔒 Khóa tài khoản',callback_data=f'user:lock:{uid}'), InlineKeyboardButton('🔓 Mở khóa',callback_data=f'user:unlock:{uid}')],
        [InlineKeyboardButton('↩️ Quay lại',callback_data='adm:menu')],
    ])
    await msg.reply_text(
        f"👤 THÔNG TIN NGƯỜI DÙNG\n\n🆔 ID: {uid}\n📛 Username: {escape(uname)}\n👤 Tên: {escape(r['full_name'] or 'Không có')}\n💰 Số dư: {r['balance']:,}đ\n🔐 Trạng thái: {'🔒 Đã khóa' if r['is_locked'] else '✅ Hoạt động'}\n📅 Tham gia: {escape(joined)}\n🕒 Hoạt động gần nhất: {escape(seen)}{profile}",
        reply_markup=kb, parse_mode='HTML'
    )

async def notify_admins(context, deposit_id, amount, memo, full_name, uid, username):
    username_text='@'+username if username else 'Không có'
    body=(f"💳 YÊU CẦU XÁC NHẬN NẠP TIỀN\n\n🧾 Mã đơn: #{deposit_id}\n💰 Số tiền: {amount:,}đ\n📝 Nội dung CK: {memo}\n👤 Tên: {escape(full_name or 'Không có')}\n🆔 ID: {uid}\n📛 Username: {escape(username_text)}\n🕒 Thời gian xác nhận: {now_vn()} (GMT+7)\n\nKiểm tra giao dịch ngân hàng trước khi duyệt.")
    kb=InlineKeyboardMarkup([[InlineKeyboardButton('✅ DUYỆT',callback_data=f'deposit:approve:{deposit_id}'), InlineKeyboardButton('❌ TỪ CHỐI',callback_data=f'deposit:reject:{deposit_id}')]])
    for admin_id in ADMIN_IDS:
        try: await context.bot.send_message(chat_id=admin_id,text=body,reply_markup=kb,parse_mode='HTML')
        except Exception: log.exception('Cannot notify admin %s about deposit %s',admin_id,deposit_id)

async def handle_deposit_review(q, context, data):
    _, action, raw_id = data.split(':', 2)
    deposit_id=int(raw_id)
    try:
        with db() as con:
            dep=con.execute('SELECT * FROM deposits WHERE id=? FOR UPDATE',(deposit_id,)).fetchone()
            if not dep:
                await q.edit_message_text('Không tìm thấy đơn nạp tiền.'); return
            if dep['status'] not in ('pending','submitted'):
                await q.edit_message_text(f"Đơn #{deposit_id} đã được xử lý trước đó: {dep['status']}."); return
            if action=='approve':
                con.execute('UPDATE users SET balance=balance+? WHERE user_id=?',(dep['amount'],dep['user_id']))
                row=con.execute('SELECT balance FROM users WHERE user_id=?',(dep['user_id'],)).fetchone()
                con.execute("INSERT INTO balance_transactions(user_id,admin_id,action,amount,balance_after,note,created_at) VALUES(?,?,'deposit',?,?,?,?)",(dep['user_id'],q.from_user.id,dep['amount'],row['balance'],f"Duyệt nạp #{deposit_id}",now()))
                con.execute("UPDATE deposits SET status='approved' WHERE id=?",(deposit_id,))
                result=f"✅ Đã duyệt đơn nạp #{deposit_id}. Đã cộng {dep['amount']:,}đ. Số dư mới: {row['balance']:,}đ."
                user_notice=f"✅ Yêu cầu nạp tiền #{deposit_id} đã được admin duyệt. Số tiền {dep['amount']:,}đ đã cộng vào tài khoản."
            else:
                con.execute("UPDATE deposits SET status='rejected' WHERE id=?",(deposit_id,))
                result=f"❌ Đã từ chối đơn nạp #{deposit_id}."
                user_notice=f"❌ Yêu cầu nạp tiền #{deposit_id} đã bị từ chối. Nếu bạn đã chuyển tiền, vui lòng liên hệ admin."
        await q.edit_message_text(result)
        try: await context.bot.send_message(chat_id=dep['user_id'],text=user_notice)
        except Exception: log.exception('Cannot notify user about deposit %s',deposit_id)
    except Exception:
        log.exception('Deposit review failed for %s',deposit_id)
        await q.message.reply_text('Có lỗi khi xử lý đơn nạp. Kiểm tra Logs trước khi thao tác lại.')

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg=update.message; text=(msg.text or '').strip(); flow=context.user_data.get('flow'); user=update.effective_user
    if text=='↩️ Trở về':
        context.user_data.clear()
        await send_main_menu(msg,user)
        return
    if user and not is_admin(update):
        if maintenance_enabled():
            await msg.reply_text('🛠 Bot đang bảo trì. Vui lòng quay lại sau.',reply_markup=USER_KB); return
        with db() as con:
            locked=con.execute('SELECT is_locked FROM users WHERE user_id=?',(user.id,)).fetchone()
        if locked and locked['is_locked']:
            await msg.reply_text('⛔ Tài khoản của bạn đang bị khóa.',reply_markup=USER_KB); return
    if flow=='custom_quantity':
        if not text.isdigit() or not 1<=int(text)<=1000: await msg.reply_text('Nhập số nguyên từ 1 đến 1.000.'); return
        oid=context.user_data.get('selected_option'); context.user_data['flow']=None
        with db() as con:
            o=con.execute('SELECT po.*,p.name AS product_name,c.name AS category_name FROM product_options po JOIN products p ON p.id::text=po.product_id::text JOIN categories c ON c.id::text=p.category_id::text WHERE po.id=?',(oid,)).fetchone()
        if not o: await msg.reply_text('Gói sản phẩm không còn tồn tại.',reply_markup=USER_KB); return
        qty=int(text); context.user_data['pending_purchase']={'option_id':oid,'quantity':qty,'total':o['price']*qty}
        p=context.user_data['pending_purchase']
        await msg.reply_text(f"🧾 XÁC NHẬN MUA HÀNG\n\n📁 Danh mục: {escape(o['category_name'])}\n📦 Sản phẩm: {escape(o['product_name'])}\n⏳ Thời hạn: {escape(o['duration_label'])}\n🔢 Số lượng: {p['quantity']}\n💰 Tổng tiền: {p['total']:,}đ\n\nBot sẽ kiểm tra kho và số dư trước khi trừ tiền.",parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('✅ XÁC NHẬN MUA',callback_data='shop:confirm')],[InlineKeyboardButton('❌ HỦY',callback_data='shop:cancel')],[InlineKeyboardButton('↩️ Trở về',callback_data='menu:home')]])); return
    if flow=='deposit_amount':
        if not text.isdigit() or not 1 <= int(text) <= 1_000_000_000:
            await msg.reply_text('Số tiền không hợp lệ. Nhập số nguyên từ 1 đến 1.000.000.000 VNĐ:'); return
        amount=int(text); uid=user.id
        try:
            with db() as con:
                row=con.execute('SELECT last_deposit_request_at,is_locked,username,full_name FROM users WHERE user_id=? FOR UPDATE',(uid,)).fetchone()
                if not row or row['is_locked']:
                    await msg.reply_text('⛔ Tài khoản không khả dụng.'); return
                last=row['last_deposit_request_at']
                if last:
                    elapsed=(datetime.now(timezone.utc)-last).total_seconds()
                    if elapsed < 30:
                        await msg.reply_text(f'⏳ Bạn cần chờ {max(1,int(30-elapsed))} giây nữa mới tạo được đơn nạp mới.'); return
                dep=con.execute("INSERT INTO deposits(user_id,amount,status,note,created_at) VALUES(?,?,'pending',NULL,?) RETURNING id",(uid,amount,now())).fetchone()
                deposit_id=dep['id']; memo=f'{QR_MEMO_PREFIX}{uid}{QR_MEMO_SUFFIX}{deposit_id}'
                con.execute('UPDATE deposits SET note=? WHERE id=?',(memo,deposit_id))
                con.execute('UPDATE users SET last_deposit_request_at=? WHERE user_id=?',(datetime.now(timezone.utc),uid))
            context.user_data['flow']=None
            qr_url=make_qr_url(amount,memo)
            caption=(f'💳 THÔNG TIN NẠP TIỀN\n\n🧾 Mã đơn: #{deposit_id}\n💰 Số tiền: {amount:,}đ\n🏦 Ngân hàng: {QR_BANK}\n📝 Nội dung chuyển khoản: {memo}\n\nQuét mã QR để chuyển khoản đúng số tiền và nội dung. Sau khi chuyển xong, bấm “Đã chuyển tiền”.')
            kb=InlineKeyboardMarkup([[InlineKeyboardButton('✅ Đã chuyển tiền',callback_data=f'deposit:submitted:{deposit_id}')],[InlineKeyboardButton('❌ Hủy',callback_data=f'deposit:cancel:{deposit_id}')]])
            await msg.reply_photo(photo=qr_url,caption=caption,reply_markup=kb)
        except Exception:
            log.exception('Create deposit request failed')
            await msg.reply_text('⚠️ Không tạo được mã QR lúc này. Vui lòng thử lại sau.')
        return
    if flow=='admin_balance_amount' and is_admin(update):
        if not text.isdigit() or not 1 <= int(text) <= 1_000_000_000:
            await msg.reply_text('Nhập số tiền nguyên dương, tối đa 1.000.000.000 VNĐ:'); return
        amount=int(text); uid=int(context.user_data.get('target_user_id') or 0); action=context.user_data.get('balance_action')
        try:
            with db() as con:
                row=con.execute('SELECT balance FROM users WHERE user_id=? FOR UPDATE',(uid,)).fetchone()
                if not row:
                    context.user_data.clear(); await msg.reply_text('Không tìm thấy người dùng.',reply_markup=ADMIN_KB); return
                if action=='subtract' and row['balance'] < amount:
                    await msg.reply_text(f"Số dư hiện tại chỉ có {row['balance']:,}đ. Nhập số tiền nhỏ hơn hoặc bằng số dư:"); return
                if action=='add':
                    con.execute('UPDATE users SET balance=balance+? WHERE user_id=?',(amount,uid))
                else:
                    con.execute('UPDATE users SET balance=balance-? WHERE user_id=?',(amount,uid))
                after=con.execute('SELECT balance FROM users WHERE user_id=?',(uid,)).fetchone()['balance']
                con.execute('INSERT INTO balance_transactions(user_id,admin_id,action,amount,balance_after,note,created_at) VALUES(?,?,?,?,?,?,?)',(uid,user.id,action,amount,after,'Admin điều chỉnh số dư',now()))
            context.user_data.clear()
            verb='cộng' if action=='add' else 'trừ'
            await msg.reply_text(f'✅ Đã {verb} {amount:,}đ cho người dùng {uid}.\nSố dư mới: {after:,}đ.',reply_markup=ADMIN_KB)
            try: await context.bot.send_message(chat_id=uid,text=f'💰 Admin đã {verb} {amount:,}đ vào số dư của bạn. Số dư hiện tại: {after:,}đ.')
            except Exception: pass
        except Exception:
            log.exception('Admin balance adjustment failed')
            await msg.reply_text('⚠️ Không thể điều chỉnh số dư. Kiểm tra Logs.',reply_markup=ADMIN_KB)
        return
    if flow=='rename_category' and is_admin(update):
        cid=context.user_data.get('rename_category_id')
        try:
            with db() as con: con.execute('UPDATE categories SET name=? WHERE id=?',(text,cid))
        except Exception:
            log.exception('Rename category failed'); await msg.reply_text('Tên đã tồn tại hoặc không thể cập nhật.'); return
        context.user_data.clear(); await msg.reply_text('✅ Đã đổi tên danh mục ngoài.',reply_markup=ADMIN_KB); return
    if flow=='edit_option_price' and is_admin(update):
        if not text.isdigit() or int(text)<0:
            await msg.reply_text('Giá phải là số nguyên không âm. Nhập lại giá VNĐ:'); return
        context.user_data['new_option_price']=int(text); context.user_data['flow']='edit_option_label'
        await msg.reply_text('Nhập tên thời hạn muốn hiển thị (ví dụ: 1 Ngày hoặc 1 Giờ):'); return
    if flow=='edit_option_label' and is_admin(update):
        oid=context.user_data.get('edit_option_id'); price=context.user_data.get('new_option_price')
        if not text or len(text)>40:
            await msg.reply_text('Tên thời hạn không hợp lệ.'); return
        with db() as con: con.execute('UPDATE product_options SET price=?,duration_label=? WHERE id=?',(price,text,oid))
        context.user_data.clear(); await msg.reply_text('✅ Đã cập nhật giá và thời hạn thành công.',reply_markup=ADMIN_KB); return
    if flow=='add_category_name' and is_admin(update):
        with db() as con: con.execute('INSERT INTO categories(name,created_by,created_at) VALUES(?,?,?) ON CONFLICT(name) DO NOTHING',(text,user.id,now()))
        context.user_data.clear(); await msg.reply_text('✅ Đã thêm danh mục ngoài.',reply_markup=ADMIN_KB); return
    if flow=='add_product_name' and is_admin(update):
        cid=context.user_data.get('new_product_category')
        if not cid:
            await msg.reply_text('⚠️ Bạn chưa chọn danh mục. Vui lòng bấm /admin để thực hiện lại.', reply_markup=ADMIN_KB)
            context.user_data.clear(); return
        if not text or len(text)>100:
            await msg.reply_text('Tên sản phẩm không hợp lệ. Nhập lại (tối đa 100 ký tự):'); return
        try:
            with db() as con:
                p=con.execute('INSERT INTO products(category_id,name,created_by,created_at) VALUES(?,?,?,?) RETURNING id',(cid,text,user.id,now())).fetchone()
                con.execute("INSERT INTO product_options(product_id,duration,duration_label,price) VALUES(?,'1h','1 giờ',0),(?,'1d','1 ngày',0),(?,'7d','7 ngày',0),(?,'30d','30 ngày',0)",(p['id'],p['id'],p['id'],p['id']))
            context.user_data.clear()
            await msg.reply_text(f'✅ Thêm sản phẩm "{text}" thành công!\n\nĐã tạo 4 gói thời hạn. Tiếp theo vào /admin → Gán ID ShopHenry cho sản phẩm cũ → chọn sản phẩm → gán ID riêng cho từng gói thời hạn. Sau đó vào Sửa giá / thời hạn để đặt giá bán.', reply_markup=ADMIN_KB)
        except Exception:
            log.exception('Add product failed')
            context.user_data.clear(); await msg.reply_text('❌ Không thể thêm sản phẩm. Kiểm tra Logs.',reply_markup=ADMIN_KB)
        return
    if flow=='map_shophenry_id' and is_admin(update):
        if not text.isdigit():
            await msg.reply_text('ID ShopHenry phải là số. Nhập lại product_id:'); return
        oid=context.user_data.get('shophenry_target_option')
        try:
            with db() as con: con.execute('UPDATE product_options SET shophenry_product_id=? WHERE id=?',(text,oid))
            context.user_data.clear(); await msg.reply_text(f'✅ Đã gán ID ShopHenry {text} cho gói sản phẩm.',reply_markup=ADMIN_KB)
        except Exception:
            log.exception('Map ShopHenry option failed'); context.user_data.clear()
            await msg.reply_text('❌ Không thể gán ID ShopHenry. Kiểm tra Logs.',reply_markup=ADMIN_KB)
        return
    if flow=='broadcast' and is_admin(update):
        context.user_data.clear(); n=await broadcast(context.bot,'📢 THÔNG BÁO TỪ SHOP\n\n'+text); await msg.reply_text(f'✅ Đã gửi thông báo tới {n} người dùng.',reply_markup=ADMIN_KB); return
    if flow=='user_check_id' and is_admin(update):
        with db() as con:
            if text.startswith('@') or not text.isdigit():
                handle=text.lstrip('@').strip()
                r=con.execute('SELECT * FROM users WHERE LOWER(username)=LOWER(?) ORDER BY last_seen DESC LIMIT 1',(handle,)).fetchone()
            else:
                r=con.execute('SELECT * FROM users WHERE user_id=?',(int(text),)).fetchone()
        context.user_data.clear()
        if not r: await msg.reply_text('Không tìm thấy người dùng. Kiểm tra ID hoặc @username.',reply_markup=ADMIN_KB); return
        await show_admin_user(msg,r['user_id']); return
    if flow=='add_product_keys' and is_admin(update):
        oid=context.user_data.get('key_option_id'); keys=clean_lines(text); added=0
        if not keys: await msg.reply_text('Không tìm thấy key hợp lệ. Gửi lại mỗi key một dòng.'); return
        with db() as con:
            for key in keys:
                cur=con.execute("INSERT INTO product_keys(option_id,key_value,added_by,added_at) VALUES(?,?,?,?) ON CONFLICT(key_value) DO NOTHING",(oid,key,user.id,now())); added+=cur.rowcount
            info=con.execute('SELECT p.name AS product_name,c.name AS category_name,po.duration_label FROM product_options po JOIN products p ON p.id::text=po.product_id::text JOIN categories c ON c.id::text=p.category_id::text WHERE po.id=?',(oid,)).fetchone()
            stock=con.execute("SELECT COUNT(*) AS n FROM product_keys WHERE option_id=? AND status='available'",(oid,)).fetchone()['n']
        context.user_data.clear()
        await msg.reply_text(f'✅ Đã thêm {added}/{len(keys)} key thành công. Kho hiện có: {stock} key.',reply_markup=ADMIN_KB)
        if added:
            announcement=(f"🔥 CÓ HÀNG MỚI VỀ! 🔥\n━━━━━━━━━━━━━━━━━━\n📁 Danh mục: {info['category_name']}\n🛍 Sản phẩm: {info['product_name']} – {info['duration_label']}\n🔢 Số lượng: {added} sản phẩm!\n\n👉 Gõ /start để vào mua ngay!")
            sent=await broadcast(context.bot,announcement)
            await msg.reply_text(f'📢 Đã thông báo tới {sent} người dùng.')
        return
    
    if is_admin(update):
        await msg.reply_text('🛠 Bạn đang không ở luồng nhập liệu nào. Vui lòng bấm /admin để mở bảng quản trị.', reply_markup=ADMIN_KB)
    else:
        await msg.reply_text('Vui lòng gửi /start để mở menu chính.')

async def error_handler(update,context): log.exception('Unhandled error',exc_info=context.error)

def main():
    if not BOT_TOKEN: raise SystemExit('Thiếu BOT_TOKEN trong .env')
    if not DATABASE_URL: raise SystemExit('Thiếu DATABASE_URL trong .env')
    if not ADMIN_IDS: raise SystemExit('Thiếu ADMIN_IDS trong .env')
    try:
        init_db()
        log.info('Database schema initialized')
    except Exception:
        log.exception('Database initialization failed')
        raise SystemExit('Database initialization failed; check Paella Logs')
    app=Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start',start)); app.add_handler(CommandHandler('admin',admin_cmd))
    app.add_handler(CallbackQueryHandler(callback)); app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_handler))
    app.add_error_handler(error_handler); log.info('Shop bot started with PostgreSQL'); app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=='__main__': main()
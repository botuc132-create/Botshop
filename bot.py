# -*- coding: utf-8 -*-
import os
import json
import logging
import sqlite3
import asyncio
import urllib.request
import urllib.error
from datetime import datetime
from html import escape

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ==================== CẤU HÌNH ====================
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
log = logging.getLogger('JinnyShopBot')

BOT_TOKEN = os.getenv('BOT_TOKEN', '7942858804:AAEqPylx7U300w3d1_U8q5LTPHh0a_o5R30').strip()
WEBAPP_URL = os.getenv('WEBAPP_URL', '').strip()
SHOPHENRY_API_TOKEN = os.getenv('SHOPHENRY_API_TOKEN', 'sk_live_3f0cefe3182f2c54895460ae01a13351').strip()
SHOPHENRY_API_URL = os.getenv('SHOPHENRY_API_URL', 'https://shophenry.io.vn/api/seller.php').strip()

# ==================== DATABASE ====================
DB_PATH = 'bot_database.db'

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def init_db():
    with db() as con:
        con.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER DEFAULT 0,
            is_locked BOOLEAN DEFAULT 0,
            is_seller BOOLEAN DEFAULT 0,
            created_at TEXT
        )''')
        con.execute('''CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            category_name TEXT,
            product_name TEXT,
            duration_label TEXT,
            quantity INTEGER,
            unit_price INTEGER,
            total_price INTEGER,
            keys_text TEXT,
            supplier_order_code TEXT,
            created_at TEXT
        )''')
        con.commit()

init_db()

# ==================== SHOPHENRY API ====================
def shophenry_fetch_freefire_products():
    if not SHOPHENRY_API_TOKEN:
        return []
    url = SHOPHENRY_API_URL + ('&' if '?' in SHOPHENRY_API_URL else '?') + 'action=products'
    req = urllib.request.Request(
        url,
        method='GET',
        headers={
            'Authorization': f'Bearer {SHOPHENRY_API_TOKEN}',
            'Accept': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except Exception as e:
        log.warning('Không thể lấy danh sách sản phẩm từ ShopHenry: %s', e)
        return []

    raw_items = payload.get('data') or payload.get('products') or []
    if not isinstance(raw_items, list):
        return []

    ff_items = []
    for item in raw_items:
        name = str(item.get('name') or item.get('title') or '')
        cat = str(item.get('category') or item.get('category_name') or '')
        full_text = f"{cat} {name}".lower()
        if 'free fire' in full_text or 'freefire' in full_text or ' ff' in full_text or full_text.startswith('ff'):
            ff_items.append({
                'shophenry_id': str(item.get('id')),
                'name': name,
                'duration_label': str(item.get('duration') or item.get('duration_label') or name),
                'price': int(float(item.get('price') or 0)),
                'stock': int(item.get('stock') or item.get('quantity') or 0),
            })
    return ff_items

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
            'User-Agent': 'Mozilla/5.0'
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
    supplier_order = str(data.get('order_code') or data.get('order_id') or data.get('id') or '')
    if isinstance(raw_keys, list):
        key_text = '\n'.join(str(k) for k in raw_keys)
    elif isinstance(raw_keys, str):
        key_text = raw_keys
    else:
        key_text = str(data)
    return key_text, supplier_order

# ==================== FASTAPI MINI APP SERVER ====================
web_app = FastAPI(title='Free Fire Shop Mini App')

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Free Fire Shop</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        :root { --bg: #070d19; --card: #0f1c30; --accent: #22c55e; --text: #ffffff; --muted: #94a3b8; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 15px; }
        h2 { text-align: center; margin-bottom: 20px; color: #38bdf8; }
        .balance-box { background: var(--card); padding: 15px; border-radius: 12px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; border: 1px solid #1e293b; }
        .product-card { background: var(--card); border: 1px solid #1e293b; border-radius: 12px; padding: 15px; margin-bottom: 15px; display: flex; justify-content: space-between; align-items: center; }
        .pkg-info h5 { margin: 0 0 5px 0; font-size: 16px; }
        .pkg-info p { margin: 0; font-size: 13px; color: var(--muted); }
        .pkg-right { text-align: right; }
        .price { font-weight: bold; color: #38bdf8; margin-bottom: 8px; }
        button { background: #2563eb; color: white; border: none; padding: 8px 16px; border-radius: 8px; font-weight: bold; cursor: pointer; }
        button:active { opacity: 0.8; }
        .loading { text-align: center; color: var(--muted); padding: 20px; }
    </style>
</head>
<body>
    <h2>🔥 FREE FIRE SHOP 🔥</h2>
    <div class="balance-box">
        <div>
            <div style="font-size: 12px; color: var(--muted);">Số dư tài khoản</div>
            <div id="user-balance" style="font-size: 18px; font-weight: bold;">Đang tải...</div>
        </div>
        <div id="user-id" style="font-size: 12px; color: var(--muted);">ID: ...</div>
    </div>

    <div id="products-list" class="loading">Đang tải danh sách sản phẩm từ nguồn...</div>

    <script>
        const tg = window.Telegram.WebApp;
        tg.expand();
        const user = tg.initDataUnsafe?.user || { id: 123456, first_name: 'Khách' };
        document.getElementById('user-id').innerText = 'ID: ' + user.id;

        async function loadData() {
            try {
                const resMe = await fetch('/api/me?user_id=' + user.id);
                const me = await resMe.json();
                document.getElementById('user-balance').innerText = (me.balance || 0).toLocaleString('vi-VN') + 'đ';

                const resCat = await fetch('/api/catalog');
                const catalog = await resCat.json();
                const list = document.getElementById('products-list');
                list.innerHTML = '';

                const cats = catalog.categories || [];
                if (!cats.length || !cats[0].products.length) {
                    list.innerHTML = '<p class="loading">Hiện không có sản phẩm nào.</p>';
                    return;
                }

                cats[0].products.forEach(product => {
                    const heading = document.createElement('h4');
                    heading.style.margin = '15px 0 8px 0';
                    heading.style.color = '#facc15';
                    heading.textContent = product.name;
                    list.appendChild(heading);

                    (product.options || []).forEach(opt => {
                        const card = document.createElement('div');
                        card.className = 'product-card';
                        card.innerHTML = `
                            <div class="pkg-info">
                                <h5>${opt.duration_label}</h5>
                                <p>Kho còn: <span style="color: ${opt.stock > 0 ? '#22c55e' : '#ef4444'}; font-weight: bold;">${opt.stock}</span></p>
                            </div>
                            <div class="pkg-right">
                                <div class="price">${Number(opt.price).toLocaleString('vi-VN')}đ</div>
                                <button onclick="buyProduct(${opt.id}, '${product.name} - ${opt.duration_label}', ${opt.price})">MUA NGAY</button>
                            </div>
                        `;
                        list.appendChild(card);
                    });
                });
            } catch (e) {
                document.getElementById('products-list').innerHTML = '<p class="loading" style="color: #ef4444;">Lỗi kết nối dữ liệu cửa hàng.</p>';
            }
        }

        async function buyProduct(optionId, title, price) {
            if (!confirm(`Xác nhận mua gói [${title}] với giá ${price.toLocaleString('vi-VN')}đ?`)) return;
            tg.MainButton.showProgress();
            try {
                const res = await fetch('/api/purchases', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ user_id: user.id, option_id: optionId, quantity: 1 })
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data.detail || 'Lỗi mua hàng');
                
                alert(`🎉 MUA HÀNG THÀNH CÔNG!\n\n🔑 KEY CỦA BẠN:\n${data.keys.join('\\n')}`);
                loadData();
            } catch (err) {
                alert('❌ ' + err.message);
            } finally {
                tg.MainButton.hideProgress();
            }
        }

        loadData();
    </script>
</body>
</html>"""

@web_app.get('/', response_class=HTMLResponse)
def index_page():
    return HTML_TEMPLATE

@web_app.get('/api/me')
def api_me(user_id: int):
    with db() as con:
        u = con.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()
        if not u:
            con.execute('INSERT OR IGNORE INTO users(user_id, balance, created_at) VALUES(?, 1000000, ?)', (user_id, now())) # Tặng sẵn 1tr test nếu chưa có
            con.commit()
            balance = 1000000
        else:
            balance = u['balance']
    return {'balance': balance}

@web_app.get('/api/catalog')
async def api_catalog():
    ff_products = await asyncio.to_thread(shophenry_fetch_freefire_products)
    grouped = {}
    for item in ff_products:
        parts = [p.strip() for p in item['name'].split('-', 1)]
        tool_name = parts[0] if parts else item['name']
        dur_label = parts[1] if len(parts) > 1 else item['duration_label']
        
        grouped.setdefault(tool_name, []).append({
            'id': -int(item['shophenry_id']),
            'duration': dur_label,
            'duration_label': dur_label,
            'price': item['price'],
            'stock': item['stock'],
            'shophenry': True
        })

    prod_out = [
        {'id': idx + 1, 'name': tool_name, 'options': opts}
        for idx, (tool_name, opts) in enumerate(grouped.items())
    ]

    return {'categories': [{'id': 1, 'name': 'Free Fire', 'guide_url': '', 'icon': '', 'products': prod_out}]}

@web_app.post('/api/purchases')
async def api_purchase(request: Request):
    payload = await request.json()
    try:
        user_id = int(payload.get('user_id'))
        oid = int(payload.get('option_id'))
        qty = int(payload.get('quantity', 1))
    except Exception:
        raise HTTPException(status_code=400, detail='Dữ liệu mua hàng không hợp lệ.')

    if oid >= 0:
        raise HTTPException(status_code=400, detail='Gói sản phẩm không hợp lệ.')

    sh_id = str(abs(oid))
    ff_list = await asyncio.to_thread(shophenry_fetch_freefire_products)
    sh_item = next((x for x in ff_list if x['shophenry_id'] == sh_id), None)
    if not sh_item:
        raise HTTPException(status_code=404, detail='Sản phẩm không tồn tại trên nguồn.')
    if sh_item['stock'] < qty:
        raise HTTPException(status_code=400, detail=f"Kho nguồn chỉ còn {sh_item['stock']} key.")

    price = int(sh_item['price'])
    total = price * qty

    con = db()
    try:
        con.execute('BEGIN')
        u = con.execute('SELECT balance, is_locked FROM users WHERE user_id=? FOR UPDATE', (user_id,)).fetchone()
        if not u:
            con.execute('INSERT INTO users(user_id, balance, created_at) VALUES(?, 1000000, ?)', (user_id, now()))
            u = {'balance': 1000000, 'is_locked': 0}
        
        if u['is_locked']:
            raise HTTPException(status_code=403, detail='Tài khoản bị khóa.')
        if u['balance'] < total:
            raise HTTPException(status_code=400, detail=f'Số dư không đủ. Cần {total:,}đ, hiện có {u["balance"]:,}đ.')

        try:
            key_text, supplier_order = await asyncio.to_thread(shophenry_buy, sh_id, qty)
        except Exception as api_error:
            raise HTTPException(status_code=502, detail=f'ShopHenry từ chối đơn: {api_error}')

        con.execute('UPDATE users SET balance=balance-? WHERE user_id=?', (total, user_id))
        con.execute(
            '''INSERT INTO purchases(user_id, category_name, product_name, duration_label, quantity, unit_price, total_price, keys_text, created_at, supplier_order_code)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (user_id, 'Free Fire', sh_item['name'], sh_item['duration_label'], qty, price, total, key_text, now(), supplier_order or None)
        )
        con.commit()
        return {
            'success': True,
            'keys': key_text.splitlines()
        }
    except HTTPException:
        con.rollback(); raise
    except Exception:
        con.rollback()
        log.exception('Purchase failed')
        raise HTTPException(status_code=500, detail='Lỗi xử lý đơn hàng.')
    finally:
        con.close()

# ==================== TELEGRAM BOT ====================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with db() as con:
        con.execute('INSERT OR IGNORE INTO users(user_id, balance, created_at) VALUES(?, 1000000, ?)', (user.id, now()))
        con.commit()

    webapp_url = WEBAPP_URL or f"https://{update.effective_message.chat.id}.railway.app" # Hoặc domain của bạn
    keyboard = [[InlineKeyboardButton("🎮 MỞ MINI APP FREE FIRE", web_app=WebAppInfo(url=webapp_url))]]
    await update.message.reply_text(
        "👋 Chào mừng bạn đến với hệ thống mua Key Free Fire tự động!\nBấm vào nút bên dưới để mở cửa hàng:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', start_cmd))
    
    import uvicorn
    import threading

    def run_fastapi():
        port = int(os.environ.get('PORT', 8080))
        uvicorn.run(web_app, host='0.0.0.0', port=port)

    threading.Thread(target=run_fastapi, daemon=True).start()
    log.info('Bot & Mini App started successfully in 1 file.')
    app.run_polling()

if __name__ == '__main__':
    main()
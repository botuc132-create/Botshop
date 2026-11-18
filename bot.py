def auto_sync_shophenry_products():
    """Tự động đồng bộ danh mục, sản phẩm và gói thời hạn từ ShopHenry vào database"""
    if not SHOPHENRY_API_TOKEN:
        return
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
        log.warning('Auto-sync ShopHenry failed: %s', e)
        return

    raw_items = payload.get('data') or payload.get('products') or []
    if not isinstance(raw_items, list):
        return

    with db() as con:
        for item in raw_items:
            sh_id = str(item.get('id'))
            name = str(item.get('name') or item.get('title') or 'Sản phẩm')
            cat_name = str(item.get('category') or item.get('category_name') or 'Free Fire')
            price = int(float(item.get('price') or 0))
            dur_label = str(item.get('duration') or item.get('duration_label') or '1 Ngày')

            # 1. Đồng bộ Danh mục
            cat_row = con.execute('SELECT id FROM categories WHERE name=?', (cat_name,)).fetchone()
            if not cat_row:
                con.execute('INSERT INTO categories(name) VALUES(?)', (cat_name,))
                cat_id = con.execute('SELECT last_insert_rowid()').fetchone()[0]
            else:
                cat_id = cat_row['id']

            # 2. Đồng bộ Sản phẩm (tách tên sản phẩm và thời hạn)
            parts = [p.strip() for p in name.split('-', 1)]
            prod_name = parts[0] if parts else name
            
            prod_row = con.execute('SELECT id FROM products WHERE category_id=? AND name=?', (cat_id, prod_name)).fetchone()
            if not prod_row:
                con.execute('INSERT INTO products(category_id, name) VALUES(?, ?)', (cat_id, prod_name))
                prod_id = con.execute('SELECT last_insert_rowid()').fetchone()[0]
            else:
                prod_id = prod_row['id']

            # 3. Đồng bộ Gói thời hạn (Option) và gán thẳng shophenry_product_id
            opt_row = con.execute('SELECT id FROM product_options WHERE product_id=? AND duration_label=?', (prod_id, dur_label)).fetchone()
            if not opt_row:
                con.execute(
                    'INSERT INTO product_options(product_id, duration, duration_label, price, shophenry_product_id) VALUES(?, ?, ?, ?, ?)',
                    (prod_id, dur_label, dur_label, price, sh_id)
                )
            else:
                con.execute(
                    'UPDATE product_options SET price=?, shophenry_product_id=? WHERE id=?',
                    (price, sh_id, opt_row['id'])
                )
        con.commit()
from __future__ import annotations

import html
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("CASHIER_DB_PATH", BASE_DIR / "cashier.sqlite3"))
STATIC_DIR = BASE_DIR / "static"
APP_NAME = "نظام كاشير"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "3737"))
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", HOST)
LOGIN_USERNAME = "طه"
LOGIN_PASSWORD = "1"

CARTS: dict[str, dict[int, int]] = {}
AUTHENTICATED_SESSIONS: set[str] = set()
SESSION_USERS: dict[str, str] = {}

INTERFACES = (
    {
        "key": "products",
        "label": "إدارة المنتجات",
        "path": "/products",
        "description": "إضافة المنتجات وتعديل الأسعار والمخزون.",
        "get_paths": ("/products", "/items/new", "/items/edit"),
        "post_paths": ("/items/create", "/items/update", "/items/delete"),
    },
    {
        "key": "pos",
        "label": "واجهة البيع",
        "path": "/pos",
        "description": "اختيار المنتجات وإتمام عمليات البيع.",
        "get_paths": ("/pos",),
        "post_paths": ("/cart/add", "/cart/remove", "/cart/clear", "/checkout"),
    },
    {
        "key": "sales",
        "label": "سجل المبيعات",
        "path": "/sales",
        "description": "عرض الفواتير والعمليات السابقة.",
        "get_paths": ("/sales",),
        "post_paths": (),
    },
    {
        "key": "customers",
        "label": "العملاء",
        "path": "/customers",
        "description": "عرض العملاء وكشوفات البيع النقدي والدين.",
        "get_paths": ("/customers", "/customers/view"),
        "post_paths": (),
    },
    {
        "key": "users",
        "label": "إدارة المستخدمين",
        "path": "/users",
        "description": "إنشاء المستخدمين وتحديد الواجهات التي تظهر لهم.",
        "get_paths": ("/users", "/users/edit"),
        "post_paths": ("/users/create", "/users/update", "/users/delete"),
        "admin_only": True,
    },
)


def money(value: float) -> str:
    return f"{value:,.0f} د.ع"


def payment_label(payment_type: str) -> str:
    return "دين" if payment_type == "debt" else "نقد"


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def get_db(path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_session(path: Path | str = DB_PATH):
    conn = get_db(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db(path: Path | str = DB_PATH) -> None:
    with db_session(path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                sku TEXT,
                category TEXT,
                price REAL NOT NULL CHECK(price >= 0),
                stock INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0),
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER,
                total REAL NOT NULL,
                paid REAL NOT NULL,
                change_amount REAL NOT NULL,
                payment_type TEXT NOT NULL DEFAULT 'cash',
                customer_name TEXT,
                debt_amount REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS sale_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sale_id INTEGER NOT NULL,
                item_id INTEGER,
                item_name TEXT NOT NULL,
                item_price REAL NOT NULL,
                quantity INTEGER NOT NULL,
                line_total REAL NOT NULL,
                FOREIGN KEY(sale_id) REFERENCES sales(id) ON DELETE CASCADE,
                FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_permissions (
                user_id INTEGER NOT NULL,
                interface_key TEXT NOT NULL,
                PRIMARY KEY (user_id, interface_key),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )
        ensure_sales_columns(db)


def ensure_sales_columns(db: sqlite3.Connection) -> None:
    columns = {row["name"] for row in db.execute("PRAGMA table_info(sales)").fetchall()}
    if "customer_id" not in columns:
        db.execute("ALTER TABLE sales ADD COLUMN customer_id INTEGER")
    if "payment_type" not in columns:
        db.execute("ALTER TABLE sales ADD COLUMN payment_type TEXT NOT NULL DEFAULT 'cash'")
    if "customer_name" not in columns:
        db.execute("ALTER TABLE sales ADD COLUMN customer_name TEXT")
    if "debt_amount" not in columns:
        db.execute("ALTER TABLE sales ADD COLUMN debt_amount REAL NOT NULL DEFAULT 0")
    migrate_sale_customers(db)


def migrate_sale_customers(db: sqlite3.Connection) -> None:
    rows = db.execute(
        """
        SELECT DISTINCT TRIM(customer_name) AS name
        FROM sales
        WHERE customer_id IS NULL
          AND customer_name IS NOT NULL
          AND TRIM(customer_name) != ''
        """
    ).fetchall()
    for row in rows:
        customer_id = get_or_create_customer(db, str(row["name"]))
        db.execute(
            """
            UPDATE sales
            SET customer_id = ?
            WHERE customer_id IS NULL AND TRIM(customer_name) = ?
            """,
            (customer_id, row["name"]),
        )


def seed_demo_items(path: Path | str = DB_PATH) -> None:
    with db_session(path) as db:
        count = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        if count:
            return
        db.executemany(
            """
            INSERT INTO items (name, sku, category, price, stock, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                ("قهوة عربية", "DRK-001", "مشروبات", 1500, 40, now_text()),
                ("ماء معدني", "DRK-002", "مشروبات", 500, 80, now_text()),
                ("كيك شوكولاتة", "SWT-010", "حلويات", 2500, 22, now_text()),
                ("ساندويش دجاج", "FD-100", "وجبات", 3500, 18, now_text()),
            ],
        )


def list_items(db: sqlite3.Connection, search: str = "") -> list[sqlite3.Row]:
    if search:
        pattern = f"%{search}%"
        return db.execute(
            """
            SELECT * FROM items
            WHERE name LIKE ? OR sku LIKE ? OR category LIKE ?
            ORDER BY id DESC
            """,
            (pattern, pattern, pattern),
        ).fetchall()
    return db.execute("SELECT * FROM items ORDER BY id DESC").fetchall()


def get_item(db: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def create_item(
    db: sqlite3.Connection,
    name: str,
    sku: str,
    category: str,
    price: float,
    stock: int,
) -> int:
    cur = db.execute(
        """
        INSERT INTO items (name, sku, category, price, stock, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (name, sku, category, price, stock, now_text()),
    )
    return int(cur.lastrowid)


def update_item(
    db: sqlite3.Connection,
    item_id: int,
    name: str,
    sku: str,
    category: str,
    price: float,
    stock: int,
) -> None:
    db.execute(
        """
        UPDATE items
        SET name = ?, sku = ?, category = ?, price = ?, stock = ?
        WHERE id = ?
        """,
        (name, sku, category, price, stock, item_id),
    )


def delete_item(db: sqlite3.Connection, item_id: int) -> None:
    db.execute("DELETE FROM items WHERE id = ?", (item_id,))


def interface_by_key(key: str) -> dict[str, object] | None:
    for interface in INTERFACES:
        if interface["key"] == key:
            return interface
    return None


def all_interface_keys() -> set[str]:
    return {str(interface["key"]) for interface in INTERFACES}


def grantable_interfaces() -> list[dict[str, object]]:
    return [interface for interface in INTERFACES if not interface.get("admin_only")]


def is_admin(username: str) -> bool:
    return username == LOGIN_USERNAME


def list_users(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM users ORDER BY id DESC").fetchall()


def get_user(db: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_username(db: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def user_permission_keys(db: sqlite3.Connection, username: str) -> set[str]:
    if is_admin(username):
        return all_interface_keys()
    user = get_user_by_username(db, username)
    if not user:
        return set()
    rows = db.execute(
        "SELECT interface_key FROM user_permissions WHERE user_id = ?",
        (user["id"],),
    ).fetchall()
    valid_keys = all_interface_keys()
    return {row["interface_key"] for row in rows if row["interface_key"] in valid_keys}


def user_can_access(db: sqlite3.Connection, username: str, interface_key: str) -> bool:
    interface = interface_by_key(interface_key)
    if not interface:
        return False
    if interface.get("admin_only") and not is_admin(username):
        return False
    return interface_key in user_permission_keys(db, username)


def default_path_for_user(db: sqlite3.Connection, username: str) -> str:
    for interface in INTERFACES:
        key = str(interface["key"])
        if user_can_access(db, username, key):
            return str(interface["path"])
    return "/no-access"


def required_interface_for_path(path: str, method: str) -> str | None:
    route_key = "post_paths" if method == "POST" else "get_paths"
    for interface in INTERFACES:
        if path in interface.get(route_key, ()):
            return str(interface["key"])
    return None


def set_user_permissions(db: sqlite3.Connection, user_id: int, permissions: set[str]) -> None:
    allowed = {str(interface["key"]) for interface in grantable_interfaces()}
    selected = sorted(permissions & allowed)
    db.execute("DELETE FROM user_permissions WHERE user_id = ?", (user_id,))
    db.executemany(
        "INSERT INTO user_permissions (user_id, interface_key) VALUES (?, ?)",
        [(user_id, key) for key in selected],
    )


def create_user(db: sqlite3.Connection, username: str, password: str, permissions: set[str]) -> int:
    cur = db.execute(
        """
        INSERT INTO users (username, password, is_active, created_at)
        VALUES (?, ?, 1, ?)
        """,
        (username, password, now_text()),
    )
    user_id = int(cur.lastrowid)
    set_user_permissions(db, user_id, permissions)
    return user_id


def update_user(
    db: sqlite3.Connection,
    user_id: int,
    username: str,
    password: str,
    permissions: set[str],
) -> None:
    if password:
        db.execute(
            "UPDATE users SET username = ?, password = ? WHERE id = ?",
            (username, password, user_id),
        )
    else:
        db.execute("UPDATE users SET username = ? WHERE id = ?", (username, user_id))
    set_user_permissions(db, user_id, permissions)


def delete_user(db: sqlite3.Connection, user_id: int) -> str:
    user = get_user(db, user_id)
    if not user:
        raise ValueError("المستخدم غير موجود")
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return str(user["username"])


def get_or_create_customer(db: sqlite3.Connection, name: str) -> int:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("اسم العميل مطلوب")
    existing = db.execute(
        "SELECT id FROM customers WHERE name = ?",
        (clean_name,),
    ).fetchone()
    if existing:
        return int(existing["id"])
    cur = db.execute(
        "INSERT INTO customers (name, created_at) VALUES (?, ?)",
        (clean_name, now_text()),
    )
    return int(cur.lastrowid)


def get_customer(db: sqlite3.Connection, customer_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()


def list_customers(db: sqlite3.Connection, search: str = "") -> list[sqlite3.Row]:
    values: list[object] = []
    where = ""
    if search:
        where = "WHERE c.name LIKE ?"
        values.append(f"%{search}%")
    return db.execute(
        f"""
        SELECT
            c.*,
            COUNT(s.id) AS sale_count,
            COALESCE(SUM(s.total), 0) AS total_sales,
            COALESCE(SUM(CASE WHEN s.payment_type = 'cash' THEN s.total ELSE s.paid END), 0) AS total_paid,
            COALESCE(SUM(s.debt_amount), 0) AS total_debt,
            MAX(s.created_at) AS last_sale_at
        FROM customers c
        LEFT JOIN sales s ON s.customer_id = c.id
        {where}
        GROUP BY c.id
        ORDER BY COALESCE(MAX(s.created_at), c.created_at) DESC, c.id DESC
        """,
        values,
    ).fetchall()


def customer_sales(db: sqlite3.Connection, customer_id: int) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT *
        FROM sales
        WHERE customer_id = ?
        ORDER BY id DESC
        """,
        (customer_id,),
    ).fetchall()


def customer_totals(db: sqlite3.Connection, customer_id: int) -> sqlite3.Row:
    return db.execute(
        """
        SELECT
            COUNT(*) AS sale_count,
            COALESCE(SUM(total), 0) AS total_sales,
            COALESCE(SUM(CASE WHEN payment_type = 'cash' THEN total ELSE paid END), 0) AS total_paid,
            COALESCE(SUM(debt_amount), 0) AS total_debt
        FROM sales
        WHERE customer_id = ?
        """,
        (customer_id,),
    ).fetchone()


def recent_sales(db: sqlite3.Connection, limit: int = 8) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM sales ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def search_sales(
    db: sqlite3.Connection,
    invoice_id: str = "",
    product_name: str = "",
    sale_date: str = "",
    limit: int = 100,
) -> list[sqlite3.Row]:
    conditions = []
    values: list[object] = []
    if invoice_id:
        conditions.append("CAST(s.id AS TEXT) LIKE ?")
        values.append(f"%{invoice_id}%")
    if product_name:
        conditions.append("si.item_name LIKE ?")
        values.append(f"%{product_name}%")
    if sale_date:
        conditions.append("substr(s.created_at, 1, 10) = ?")
        values.append(sale_date)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    values.append(limit)
    return db.execute(
        f"""
        SELECT DISTINCT s.*
        FROM sales s
        LEFT JOIN sale_items si ON si.sale_id = s.id
        {where}
        ORDER BY s.id DESC
        LIMIT ?
        """,
        values,
    ).fetchall()


def sale_products_summary(db: sqlite3.Connection, sale_ids: list[int]) -> dict[int, str]:
    if not sale_ids:
        return {}
    placeholders = ",".join("?" for _ in sale_ids)
    rows = db.execute(
        f"""
        SELECT sale_id, item_name, quantity
        FROM sale_items
        WHERE sale_id IN ({placeholders})
        ORDER BY id
        """,
        sale_ids,
    ).fetchall()
    summaries: dict[int, list[str]] = {}
    for row in rows:
        summaries.setdefault(int(row["sale_id"]), []).append(
            f"{row['quantity']} × {row['item_name']}"
        )
    return {sale_id: "، ".join(items) for sale_id, items in summaries.items()}


def customer_link(customer_id: object, customer_name: object) -> str:
    if customer_id and customer_name:
        return f'<a class="table-link" href="/customers/view?id={customer_id}">{esc(customer_name)}</a>'
    return esc(customer_name or "-")


def create_sale(
    db: sqlite3.Connection,
    cart: dict[int, int],
    paid: float,
    payment_type: str = "cash",
    customer_name: str = "",
) -> tuple[int, float, float]:
    if not cart:
        raise ValueError("السلة فارغة")
    if payment_type not in {"cash", "debt"}:
        raise ValueError("طريقة الدفع غير صحيحة")

    item_ids = list(cart.keys())
    placeholders = ",".join("?" for _ in item_ids)
    rows = db.execute(
        f"SELECT * FROM items WHERE id IN ({placeholders})",
        item_ids,
    ).fetchall()
    items = {int(row["id"]): row for row in rows}

    total = 0.0
    sale_lines: list[tuple[int, str, float, int, float]] = []
    for item_id, qty in cart.items():
        if item_id not in items:
            raise ValueError("يوجد منتج غير متوفر في السلة")
        item = items[item_id]
        if qty <= 0:
            raise ValueError("الكمية غير صحيحة")
        if int(item["stock"]) < qty:
            raise ValueError(f"المخزون غير كافٍ للمنتج: {item['name']}")
        line_total = float(item["price"]) * qty
        total += line_total
        sale_lines.append((item_id, item["name"], float(item["price"]), qty, line_total))

    customer_name = customer_name.strip()
    if payment_type == "cash" and paid < total:
        raise ValueError("المبلغ المدفوع أقل من الإجمالي")
    if payment_type == "debt" and not customer_name:
        raise ValueError("اسم الزبون مطلوب عند البيع بالدين")
    if payment_type == "debt" and paid >= total:
        raise ValueError("في البيع بالدين يجب أن يكون المدفوع أقل من الإجمالي")

    change_amount = paid - total if payment_type == "cash" else 0.0
    debt_amount = total - paid if payment_type == "debt" else 0.0
    customer_id = get_or_create_customer(db, customer_name) if customer_name else None
    cur = db.execute(
        """
        INSERT INTO sales
            (customer_id, total, paid, change_amount, payment_type, customer_name, debt_amount, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            customer_id,
            total,
            paid,
            change_amount,
            payment_type,
            customer_name,
            debt_amount,
            now_text(),
        ),
    )
    sale_id = int(cur.lastrowid)

    for item_id, name, price, qty, line_total in sale_lines:
        db.execute(
            """
            INSERT INTO sale_items
                (sale_id, item_id, item_name, item_price, quantity, line_total)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (sale_id, item_id, name, price, qty, line_total),
        )
        db.execute(
            "UPDATE items SET stock = stock - ? WHERE id = ?",
            (qty, item_id),
        )

    return sale_id, total, change_amount


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def parse_float(value: str, field_name: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} يجب أن يكون رقمًا") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} لا يمكن أن يكون سالبًا")
    return parsed


def parse_int(value: str, field_name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} يجب أن يكون عددًا صحيحًا") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} لا يمكن أن يكون سالبًا")
    return parsed


def validate_item_form(data: dict[str, str]) -> tuple[str, str, str, float, int]:
    name = data.get("name", "").strip()
    sku = data.get("sku", "").strip()
    category = data.get("category", "").strip()
    if not name:
        raise ValueError("اسم المنتج مطلوب")
    price = parse_float(data.get("price", "0").strip(), "السعر")
    stock = parse_int(data.get("stock", "0").strip(), "المخزون")
    return name, sku, category, price, stock


def permissions_from_form(data: dict[str, str]) -> set[str]:
    return {
        str(interface["key"])
        for interface in grantable_interfaces()
        if data.get(f"perm_{interface['key']}") == "1"
    }


def validate_user_form(data: dict[str, str], require_password: bool = True) -> tuple[str, str, set[str]]:
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username:
        raise ValueError("اسم المستخدم مطلوب")
    if username == LOGIN_USERNAME:
        raise ValueError("طه هو حساب المدير الثابت ولا يمكن إنشاؤه أو تعديله من هنا")
    if require_password and not password:
        raise ValueError("الرمز مطلوب")
    return username, password, permissions_from_form(data)


def arabic_query(message: str) -> str:
    return quote(message, safe="")


def secure_text_equal(left: str, right: str) -> bool:
    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def login_page(error: str = "") -> str:
    return f"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>تسجيل الدخول - {APP_NAME}</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body class="login-body">
  <main class="login-shell">
    <section class="login-panel">
      <div class="login-visual">
        <div class="brand login-brand">
          <div class="brand-mark">ك</div>
          <div>
            <strong>{APP_NAME}</strong>
            <span>نقطة بيع عربية</span>
          </div>
        </div>
        <h1>أهلًا طه</h1>
        <p>ادخل إلى لوحة الكاشير لإدارة المنتجات، البيع، ومتابعة سجل الفواتير بسرعة وأناقة.</p>
        <div class="login-badges">
          <span>SQLite</span>
          <span>واجهة عربية</span>
          <span>بيع مباشر</span>
        </div>
      </div>
      <form class="login-card" method="post" action="/login" accept-charset="utf-8">
        <p class="eyebrow">دخول الموظف</p>
        <h2>تسجيل الدخول</h2>
        {notice(error, 'error')}
        <label>اسم المستخدم
          <input name="username" required autofocus autocomplete="username">
        </label>
        <label>الرمز
          <input name="password" required type="password" autocomplete="current-password">
        </label>
        <button class="btn primary wide" type="submit">دخول النظام</button>
      </form>
    </section>
  </main>
</body>
</html>"""


def page(db: sqlite3.Connection, username: str, title: str, content: str, active: str = "") -> str:
    allowed_keys = user_permission_keys(db, username)
    nav_links = "\n".join(
        f'<a class="{"active" if active == interface["key"] else ""}" href="{interface["path"]}">{esc(interface["label"])}</a>'
        for interface in INTERFACES
        if interface["key"] in allowed_keys
    )
    if not nav_links:
        nav_links = '<span class="nav-empty">لا توجد واجهات مفعّلة</span>'
    role_text = "مدير النظام" if is_admin(username) else "مستخدم"
    return f"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)} - {APP_NAME}</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <div class="shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark">ك</div>
        <div>
          <strong>{APP_NAME}</strong>
          <span>واجهة عربية خفيفة</span>
        </div>
      </div>
      <nav>
        {nav_links}
      </nav>
      <form class="logout-form" method="post" action="/logout">
        <button class="btn ghost wide" type="submit">تسجيل الخروج</button>
      </form>
      <div class="side-note">
        <span>{esc(role_text)}</span>
        <strong>{esc(username)}</strong>
      </div>
    </aside>
    <main class="main">
      {content}
    </main>
  </div>
</body>
</html>"""


def notice(message: str | None, kind: str = "success") -> str:
    if not message:
        return ""
    return f'<div class="notice {esc(kind)}">{esc(message)}</div>'


def stat_cards(db: sqlite3.Connection) -> str:
    stats = db.execute(
        """
        SELECT
            COUNT(*) AS item_count,
            COALESCE(SUM(stock), 0) AS stock_count,
            COALESCE(SUM(price * stock), 0) AS inventory_value
        FROM items
        """
    ).fetchone()
    today_sales = db.execute(
        "SELECT COALESCE(SUM(total), 0) FROM sales WHERE substr(created_at, 1, 10) = ?",
        (datetime.now().strftime("%Y-%m-%d"),),
    ).fetchone()[0]
    return f"""
    <section class="stats">
      <div class="stat"><span>عدد المنتجات</span><strong>{stats['item_count']}</strong></div>
      <div class="stat"><span>إجمالي المخزون</span><strong>{stats['stock_count']}</strong></div>
      <div class="stat"><span>قيمة المخزون</span><strong>{money(stats['inventory_value'])}</strong></div>
      <div class="stat highlight"><span>مبيعات اليوم</span><strong>{money(today_sales)}</strong></div>
    </section>
    """


def products_page(db: sqlite3.Connection, username: str, params: dict[str, list[str]]) -> str:
    search = params.get("q", [""])[0].strip()
    rows = list_items(db, search)
    msg = params.get("msg", [""])[0]
    err = params.get("err", [""])[0]
    body_rows = "\n".join(
        f"""
        <tr>
          <td><strong>{esc(row['name'])}</strong><small>{esc(row['sku'] or 'بدون باركود')}</small></td>
          <td>{esc(row['category'] or 'عام')}</td>
          <td>{money(row['price'])}</td>
          <td><span class="pill {'danger' if row['stock'] <= 5 else ''}">{row['stock']}</span></td>
          <td class="actions">
            <a class="btn small ghost" href="/items/edit?id={row['id']}">تعديل</a>
            <form method="post" action="/items/delete" onsubmit="return confirm('حذف المنتج؟')">
              <input type="hidden" name="id" value="{row['id']}">
              <button class="btn small danger" type="submit">حذف</button>
            </form>
          </td>
        </tr>
        """
        for row in rows
    )
    if not body_rows:
        body_rows = '<tr><td colspan="5" class="empty">لا توجد منتجات مطابقة.</td></tr>'

    content = f"""
    <header class="hero">
      <div>
        <p class="eyebrow">المخزن والمنتجات</p>
        <h1>إدارة المنتجات بسهولة</h1>
        <p>أضف الأصناف، عدّل الأسعار والمخزون، واحذف ما لم تعد تحتاجه.</p>
      </div>
      <a class="btn primary" href="/items/new">+ إضافة منتج</a>
    </header>
    {notice(msg)}
    {notice(err, 'error')}
    {stat_cards(db)}
    <section class="card">
      <div class="card-head">
        <h2>قائمة المنتجات</h2>
        <form class="search" method="get" action="/products">
          <input name="q" value="{esc(search)}" placeholder="ابحث بالاسم، الباركود، التصنيف">
          <button class="btn ghost" type="submit">بحث</button>
        </form>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>المنتج</th>
              <th>التصنيف</th>
              <th>السعر</th>
              <th>المخزون</th>
              <th>إجراءات</th>
            </tr>
          </thead>
          <tbody>{body_rows}</tbody>
        </table>
      </div>
    </section>
    """
    return page(db, username, "إدارة المنتجات", content, "products")


def item_form_page(db: sqlite3.Connection, username: str, item: sqlite3.Row | None = None, error: str = "") -> str:
    is_edit = item is not None
    title = "تعديل منتج" if is_edit else "إضافة منتج"
    action = "/items/update" if is_edit else "/items/create"
    hidden = f'<input type="hidden" name="id" value="{item["id"]}">' if is_edit else ""
    name = item["name"] if is_edit else ""
    sku = item["sku"] if is_edit else ""
    category = item["category"] if is_edit else ""
    price = item["price"] if is_edit else ""
    stock = item["stock"] if is_edit else ""

    content = f"""
    <header class="hero compact">
      <div>
        <p class="eyebrow">بيانات المنتج</p>
        <h1>{title}</h1>
        <p>املأ التفاصيل الأساسية، ويمكنك ترك الباركود أو التصنيف فارغًا.</p>
      </div>
    </header>
    {notice(error, 'error')}
    <section class="card form-card">
      <form method="post" action="{action}" class="item-form">
        {hidden}
        <label>اسم المنتج
          <input name="name" required value="{esc(name)}" placeholder="مثال: شاي عراقي">
        </label>
        <label>الباركود / الكود
          <input name="sku" value="{esc(sku)}" placeholder="اختياري">
        </label>
        <label>التصنيف
          <input name="category" value="{esc(category)}" placeholder="مشروبات، غذائيات...">
        </label>
        <label>السعر
          <input name="price" required type="number" min="0" step="250" value="{esc(price)}">
        </label>
        <label>المخزون
          <input name="stock" required type="number" min="0" step="1" value="{esc(stock)}">
        </label>
        <div class="form-actions">
          <button class="btn primary" type="submit">حفظ المنتج</button>
          <a class="btn ghost" href="/products">رجوع</a>
        </div>
      </form>
    </section>
    """
    return page(db, username, title, content, "products")


def cart_details(db: sqlite3.Connection, cart: dict[int, int]) -> tuple[str, float]:
    if not cart:
        return '<div class="empty cart-empty">السلة فارغة، اختر منتجات من اليمين.</div>', 0.0

    item_ids = list(cart.keys())
    placeholders = ",".join("?" for _ in item_ids)
    rows = db.execute(f"SELECT * FROM items WHERE id IN ({placeholders})", item_ids).fetchall()
    items = {int(row["id"]): row for row in rows}
    total = 0.0
    lines = []
    for item_id, qty in cart.items():
        item = items.get(item_id)
        if not item:
            continue
        line_total = float(item["price"]) * qty
        total += line_total
        lines.append(
            f"""
            <div class="cart-line">
              <div>
                <strong>{esc(item['name'])}</strong>
                <span>{qty} × {money(item['price'])}</span>
              </div>
              <div class="cart-line-actions">
                <b>{money(line_total)}</b>
                <form method="post" action="/cart/remove">
                  <input type="hidden" name="id" value="{item_id}">
                  <button class="icon-btn" title="حذف من السلة">×</button>
                </form>
              </div>
            </div>
            """
        )
    return "\n".join(lines), total


def pos_page(db: sqlite3.Connection, sid: str, username: str, params: dict[str, list[str]]) -> str:
    search = params.get("q", [""])[0].strip()
    msg = params.get("msg", [""])[0]
    err = params.get("err", [""])[0]
    cart = CARTS.setdefault(sid, {})
    items = list_items(db, search)
    product_cards = "\n".join(
        f"""
        <article class="product-tile">
          <div>
            <span>{esc(row['category'] or 'عام')}</span>
            <h3>{esc(row['name'])}</h3>
            <p>{money(row['price'])}</p>
            <small>المخزون: {row['stock']}</small>
          </div>
          <form method="post" action="/cart/add">
            <input type="hidden" name="id" value="{row['id']}">
            <input class="qty" name="qty" type="number" value="1" min="1" max="{row['stock']}">
            <button class="btn small primary" {'disabled' if row['stock'] <= 0 else ''}>إضافة</button>
          </form>
        </article>
        """
        for row in items
    )
    if not product_cards:
        product_cards = '<div class="empty">لا توجد منتجات للبيع.</div>'

    cart_html, total = cart_details(db, cart)
    products_link = '<a class="btn ghost" href="/products">إدارة المنتجات</a>' if user_can_access(db, username, "products") else ""
    content = f"""
    <header class="hero">
      <div>
        <p class="eyebrow">شاشة البيع</p>
        <h1>بيع سريع وواضح</h1>
        <p>اختر المنتجات، راجع السلة، ثم أكمل البيع وسيتم خصم المخزون تلقائيًا.</p>
      </div>
      {products_link}
    </header>
    {notice(msg)}
    {notice(err, 'error')}
    <section class="pos-grid">
      <div class="card">
        <div class="card-head">
          <h2>المنتجات</h2>
          <form class="search" method="get" action="/pos">
            <input name="q" value="{esc(search)}" placeholder="بحث سريع">
            <button class="btn ghost" type="submit">بحث</button>
          </form>
        </div>
        <div class="products-grid">{product_cards}</div>
      </div>
      <aside class="card checkout">
        <h2>السلة</h2>
        <div class="cart-lines">{cart_html}</div>
        <div class="total-row">
          <span>الإجمالي</span>
          <strong>{money(total)}</strong>
        </div>
        <form method="post" action="/checkout" class="checkout-form">
          <div class="payment-options">
            <label>
              <input type="radio" name="payment_type" value="cash" checked>
              <span>نقد</span>
            </label>
            <label>
              <input type="radio" name="payment_type" value="debt">
              <span>دين</span>
            </label>
          </div>
          <label>المبلغ المدفوع
            <input name="paid" type="number" min="0" step="250" value="{int(total)}">
          </label>
          <label>اسم العميل
            <input name="customer_name" placeholder="اختياري للنقد ومطلوب عند البيع بالدين">
          </label>
          <button class="btn primary wide" type="submit" {'disabled' if total <= 0 else ''}>إتمام البيع</button>
        </form>
        <form method="post" action="/cart/clear">
          <button class="btn ghost wide" type="submit" {'disabled' if total <= 0 else ''}>تفريغ السلة</button>
        </form>
      </aside>
    </section>
    """
    return page(db, username, "واجهة البيع", content, "pos")


def sales_page(db: sqlite3.Connection, username: str, params: dict[str, list[str]]) -> str:
    msg = params.get("msg", [""])[0]
    invoice_id = params.get("invoice", [""])[0].strip()
    product_name = params.get("product", [""])[0].strip()
    sale_date = params.get("date", [""])[0].strip()
    sales = search_sales(db, invoice_id, product_name, sale_date, 100)
    product_summaries = sale_products_summary(db, [int(sale["id"]) for sale in sales])
    rows = "\n".join(
        f"""
        <tr>
          <td>#{sale['id']}</td>
          <td>{esc(sale['created_at'])}</td>
          <td>{esc(product_summaries.get(int(sale['id']), 'بدون تفاصيل'))}</td>
          <td>{payment_label(str(sale['payment_type']))}</td>
          <td>{customer_link(sale['customer_id'], sale['customer_name'])}</td>
          <td>{money(sale['total'])}</td>
          <td>{money(sale['paid'])}</td>
          <td>{money(sale['change_amount'])}</td>
          <td>{money(sale['debt_amount'])}</td>
        </tr>
        """
        for sale in sales
    )
    if not rows:
        rows = '<tr><td colspan="9" class="empty">لا توجد مبيعات مطابقة.</td></tr>'
    content = f"""
    <header class="hero compact no-print">
      <div>
        <p class="eyebrow">الأرشيف</p>
        <h1>سجل المبيعات</h1>
        <p>ابحث برقم الفاتورة، اسم المنتج، أو تاريخ البيع ثم اطبع التقرير الظاهر.</p>
      </div>
      <button class="btn primary" type="button" onclick="window.print()">طباعة التقرير</button>
    </header>
    {notice(msg)}
    <section class="card">
      <div class="print-title">
        <h2>تقرير سجل المبيعات</h2>
        <p>تاريخ الطباعة: {esc(now_text())}</p>
      </div>
      <form class="sales-filters no-print" method="get" action="/sales">
        <label>رقم الفاتورة
          <input name="invoice" value="{esc(invoice_id)}" placeholder="مثال: 12">
        </label>
        <label>اسم المنتج
          <input name="product" value="{esc(product_name)}" placeholder="مثال: شاي">
        </label>
        <label>تاريخ البيع
          <input name="date" type="date" value="{esc(sale_date)}">
        </label>
        <div class="filter-actions">
          <button class="btn ghost" type="submit">بحث</button>
          <a class="btn ghost" href="/sales">مسح</a>
          <button class="btn primary" type="button" onclick="window.print()">طباعة</button>
        </div>
      </form>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>رقم الفاتورة</th>
              <th>التاريخ</th>
              <th>المنتجات</th>
              <th>طريقة الدفع</th>
              <th>الزبون</th>
              <th>الإجمالي</th>
              <th>المدفوع</th>
              <th>الباقي</th>
              <th>الدين</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </section>
    """
    return page(db, username, "سجل المبيعات", content, "sales")


def customers_page(db: sqlite3.Connection, username: str, params: dict[str, list[str]]) -> str:
    search = params.get("q", [""])[0].strip()
    customers = list_customers(db, search)
    rows = "\n".join(
        f"""
        <tr>
          <td><a class="table-link" href="/customers/view?id={customer['id']}">{esc(customer['name'])}</a></td>
          <td>{customer['sale_count']}</td>
          <td>{money(customer['total_sales'])}</td>
          <td>{money(customer['total_paid'])}</td>
          <td>{money(customer['total_debt'])}</td>
          <td>{esc(customer['last_sale_at'] or '-')}</td>
          <td><a class="btn small ghost" href="/customers/view?id={customer['id']}">الكشف</a></td>
        </tr>
        """
        for customer in customers
    )
    if not rows:
        rows = '<tr><td colspan="7" class="empty">لا يوجد عملاء مطابقون.</td></tr>'
    content = f"""
    <header class="hero compact no-print">
      <div>
        <p class="eyebrow">دفتر العملاء</p>
        <h1>العملاء وكشوفاتهم</h1>
        <p>تظهر هنا أسماء العملاء الذين تم تسجيل أسمائهم في البيع النقدي أو البيع بالدين.</p>
      </div>
    </header>
    <section class="card">
      <div class="card-head">
        <h2>قائمة العملاء</h2>
        <form class="search" method="get" action="/customers">
          <input name="q" value="{esc(search)}" placeholder="ابحث باسم العميل">
          <button class="btn ghost" type="submit">بحث</button>
        </form>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>العميل</th>
              <th>عدد الفواتير</th>
              <th>إجمالي المشتريات</th>
              <th>إجمالي المدفوع</th>
              <th>الدين الحالي</th>
              <th>آخر عملية</th>
              <th>إجراءات</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </section>
    """
    return page(db, username, "العملاء", content, "customers")


def customer_statement_page(db: sqlite3.Connection, username: str, customer_id: int) -> str:
    customer = get_customer(db, customer_id)
    if not customer:
        content = """
        <header class="hero compact">
          <div>
            <p class="eyebrow">العملاء</p>
            <h1>العميل غير موجود</h1>
            <p>قد يكون تم حذف العميل أو أن الرابط غير صحيح.</p>
          </div>
          <a class="btn ghost" href="/customers">رجوع</a>
        </header>
        """
        return page(db, username, "العميل غير موجود", content, "customers")

    sales = customer_sales(db, customer_id)
    totals = customer_totals(db, customer_id)
    product_summaries = sale_products_summary(db, [int(sale["id"]) for sale in sales])
    rows = "\n".join(
        f"""
        <tr>
          <td>#{sale['id']}</td>
          <td>{esc(sale['created_at'])}</td>
          <td>{esc(product_summaries.get(int(sale['id']), 'بدون تفاصيل'))}</td>
          <td>{payment_label(str(sale['payment_type']))}</td>
          <td>{money(sale['total'])}</td>
          <td>{money(sale['paid'])}</td>
          <td>{money(sale['change_amount'])}</td>
          <td>{money(sale['debt_amount'])}</td>
        </tr>
        """
        for sale in sales
    )
    if not rows:
        rows = '<tr><td colspan="8" class="empty">لا توجد فواتير لهذا العميل.</td></tr>'

    content = f"""
    <header class="hero compact no-print">
      <div>
        <p class="eyebrow">كشف عميل</p>
        <h1>{esc(customer['name'])}</h1>
        <p>كشف تفصيلي بكل عمليات البيع النقدي والدين المسجلة على هذا العميل.</p>
      </div>
      <div class="actions">
        <button class="btn primary" type="button" onclick="window.print()">طباعة الكشف</button>
        <a class="btn ghost" href="/customers">رجوع</a>
      </div>
    </header>
    <section class="stats">
      <div class="stat"><span>عدد الفواتير</span><strong>{totals['sale_count']}</strong></div>
      <div class="stat"><span>إجمالي المشتريات</span><strong>{money(totals['total_sales'])}</strong></div>
      <div class="stat"><span>إجمالي المدفوع</span><strong>{money(totals['total_paid'])}</strong></div>
      <div class="stat highlight"><span>الدين الحالي</span><strong>{money(totals['total_debt'])}</strong></div>
    </section>
    <section class="card">
      <div class="print-title">
        <h2>كشف العميل: {esc(customer['name'])}</h2>
        <p>تاريخ الطباعة: {esc(now_text())}</p>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>رقم الفاتورة</th>
              <th>التاريخ</th>
              <th>المنتجات</th>
              <th>طريقة الدفع</th>
              <th>الإجمالي</th>
              <th>المدفوع</th>
              <th>الباقي</th>
              <th>الدين</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </section>
    """
    return page(db, username, f"كشف {customer['name']}", content, "customers")


def permission_badges(keys: set[str]) -> str:
    labels = [
        str(interface["label"])
        for interface in INTERFACES
        if str(interface["key"]) in keys
    ]
    if not labels:
        return '<span class="muted-line">لا توجد صلاحيات</span>'
    return " ".join(f'<span class="pill">{esc(label)}</span>' for label in labels)


def permission_checkboxes(selected: set[str]) -> str:
    return "\n".join(
        f"""
        <label class="check-card">
          <input type="checkbox" name="perm_{esc(interface['key'])}" value="1" {"checked" if interface["key"] in selected else ""}>
          <span>
            <strong>{esc(interface["label"])}</strong>
            <small>{esc(interface["description"])}</small>
          </span>
        </label>
        """
        for interface in grantable_interfaces()
    )


def users_page(
    db: sqlite3.Connection,
    username: str,
    params: dict[str, list[str]],
    edit_user: sqlite3.Row | None = None,
) -> str:
    msg = params.get("msg", [""])[0]
    err = params.get("err", [""])[0]
    is_edit = edit_user is not None
    form_title = "تعديل مستخدم" if is_edit else "إضافة مستخدم"
    form_action = "/users/update" if is_edit else "/users/create"
    hidden = f'<input type="hidden" name="id" value="{edit_user["id"]}">' if is_edit else ""
    edited_username = edit_user["username"] if is_edit else ""
    selected = user_permission_keys(db, edited_username) if is_edit else {"products", "pos"}
    password_hint = "اتركه فارغًا إذا لا تريد تغيير الرمز" if is_edit else "مثال: 1234"
    password_required = "" if is_edit else "required"
    cancel_link = '<a class="btn ghost" href="/users">إلغاء التعديل</a>' if is_edit else ""

    admin_permissions = permission_badges(user_permission_keys(db, LOGIN_USERNAME))
    admin_row = f"""
    <tr>
      <td><strong>{LOGIN_USERNAME}</strong><small>مدير النظام الثابت</small></td>
      <td>{admin_permissions}</td>
      <td>دائم</td>
      <td><span class="role-pill">لا يمكن الحذف</span></td>
    </tr>
    """
    user_rows = "\n".join(
        f"""
        <tr>
          <td><strong>{esc(row['username'])}</strong><small>مستخدم داخل النظام</small></td>
          <td>{permission_badges(user_permission_keys(db, row['username']))}</td>
          <td>{esc(row['created_at'])}</td>
          <td class="actions">
            <a class="btn small ghost" href="/users/edit?id={row['id']}">تعديل</a>
            <form method="post" action="/users/delete" onsubmit="return confirm('حذف المستخدم؟')">
              <input type="hidden" name="id" value="{row['id']}">
              <button class="btn small danger" type="submit">حذف</button>
            </form>
          </td>
        </tr>
        """
        for row in list_users(db)
    )

    content = f"""
    <header class="hero compact">
      <div>
        <p class="eyebrow">الصلاحيات والمستخدمون</p>
        <h1>إدارة المستخدمين</h1>
        <p>أنشئ مستخدمين وحدد الواجهات التي تظهر لكل واحد منهم. المدير طه يملك كل الصلاحيات دائمًا.</p>
      </div>
    </header>
    {notice(msg)}
    {notice(err, 'error')}
    <section class="users-grid">
      <div class="card">
        <h2>{form_title}</h2>
        <form method="post" action="{form_action}" class="user-form">
          {hidden}
          <label>اسم المستخدم
            <input name="username" required value="{esc(edited_username)}" placeholder="مثال: أحمد">
          </label>
          <label>الرمز
            <input name="password" type="password" {password_required} placeholder="{esc(password_hint)}">
          </label>
          <div class="form-block">
            <strong>الواجهات المسموحة</strong>
            <div class="permission-grid">
              {permission_checkboxes(selected)}
            </div>
          </div>
          <div class="form-actions">
            <button class="btn primary" type="submit">حفظ المستخدم</button>
            {cancel_link}
          </div>
        </form>
      </div>
      <div class="card">
        <div class="card-head">
          <h2>المستخدمون</h2>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>المستخدم</th>
                <th>الصلاحيات</th>
                <th>تاريخ الإضافة</th>
                <th>إجراءات</th>
              </tr>
            </thead>
            <tbody>{admin_row}{user_rows}</tbody>
          </table>
        </div>
      </div>
    </section>
    """
    return page(db, username, "إدارة المستخدمين", content, "users")


def forbidden_page(db: sqlite3.Connection, username: str) -> str:
    content = """
    <header class="hero compact">
      <div>
        <p class="eyebrow">صلاحية غير متاحة</p>
        <h1>لا تملك صلاحية لهذه الواجهة</h1>
        <p>تواصل مع المدير طه لتفعيل هذه الواجهة لحسابك.</p>
      </div>
    </header>
    """
    return page(db, username, "غير مسموح", content)


def no_access_page(db: sqlite3.Connection, username: str) -> str:
    content = """
    <header class="hero compact">
      <div>
        <p class="eyebrow">لا توجد واجهات</p>
        <h1>لم يتم تفعيل أي واجهة لحسابك</h1>
        <p>يمكن للمدير طه تعديل صلاحياتك من واجهة إدارة المستخدمين.</p>
      </div>
    </header>
    """
    return page(db, username, "لا توجد صلاحيات", content)


class CashierHandler(BaseHTTPRequestHandler):
    server_version = "ArabicCashier/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        sid = self.ensure_session()

        if parsed.path.startswith("/static/"):
            self.serve_static(parsed.path)
            return
        if parsed.path == "/login":
            if self.is_authenticated(sid):
                with db_session() as db:
                    self.redirect(default_path_for_user(db, self.current_username(sid)))
                return
            self.respond_html(login_page(params.get("err", [""])[0]))
            return
        if not self.is_authenticated(sid):
            self.redirect("/login")
            return

        with db_session() as db:
            username = self.current_username(sid)
            if parsed.path in ("", "/"):
                self.redirect(default_path_for_user(db, username))
                return
            if parsed.path == "/no-access":
                self.respond_html(no_access_page(db, username))
                return
            required = required_interface_for_path(parsed.path, "GET")
            if required and not user_can_access(db, username, required):
                self.respond_html(forbidden_page(db, username), HTTPStatus.FORBIDDEN)
                return
            if parsed.path == "/products":
                self.respond_html(products_page(db, username, params))
                return
            if parsed.path == "/items/new":
                self.respond_html(item_form_page(db, username))
                return
            if parsed.path == "/items/edit":
                item_id = parse_int(params.get("id", ["0"])[0], "رقم المنتج")
                item = get_item(db, item_id)
                if not item:
                    self.redirect(f"/products?err={arabic_query('المنتج غير موجود')}")
                    return
                self.respond_html(item_form_page(db, username, item))
                return
            if parsed.path == "/pos":
                self.respond_html(pos_page(db, sid, username, params))
                return
            if parsed.path == "/sales":
                self.respond_html(sales_page(db, username, params))
                return
            if parsed.path == "/customers":
                self.respond_html(customers_page(db, username, params))
                return
            if parsed.path == "/customers/view":
                customer_id = parse_int(params.get("id", ["0"])[0], "رقم العميل")
                self.respond_html(customer_statement_page(db, username, customer_id))
                return
            if parsed.path == "/users":
                self.respond_html(users_page(db, username, params))
                return
            if parsed.path == "/users/edit":
                user_id = parse_int(params.get("id", ["0"])[0], "رقم المستخدم")
                edit_user = get_user(db, user_id)
                if not edit_user:
                    self.redirect(f"/users?err={arabic_query('المستخدم غير موجود')}")
                    return
                self.respond_html(users_page(db, username, params, edit_user))
                return

        self.respond_text("الصفحة غير موجودة", HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        sid = self.ensure_session()
        data = self.read_form()

        if parsed.path == "/login":
            username = data.get("username", "").strip()
            password = data.get("password", "").strip()
            with db_session() as db:
                if self.authenticate(db, username, password):
                    AUTHENTICATED_SESSIONS.add(sid)
                    SESSION_USERS[sid] = username
                    self.redirect(default_path_for_user(db, username))
                    return
            self.respond_html(login_page("اسم المستخدم أو الرمز غير صحيح"), HTTPStatus.UNAUTHORIZED)
            return
        if parsed.path == "/logout":
            AUTHENTICATED_SESSIONS.discard(sid)
            SESSION_USERS.pop(sid, None)
            CARTS.pop(sid, None)
            self.redirect("/login")
            return
        if not self.is_authenticated(sid):
            self.redirect("/login")
            return

        try:
            with db_session() as db:
                username = self.current_username(sid)
                required = required_interface_for_path(parsed.path, "POST")
                if required and not user_can_access(db, username, required):
                    self.respond_html(forbidden_page(db, username), HTTPStatus.FORBIDDEN)
                    return
                if parsed.path == "/items/create":
                    item_data = validate_item_form(data)
                    create_item(db, *item_data)
                    self.redirect(f"/products?msg={arabic_query('تمت إضافة المنتج بنجاح')}")
                    return
                if parsed.path == "/items/update":
                    item_id = parse_int(data.get("id", "0"), "رقم المنتج")
                    item_data = validate_item_form(data)
                    update_item(db, item_id, *item_data)
                    self.redirect(f"/products?msg={arabic_query('تم تحديث المنتج')}")
                    return
                if parsed.path == "/items/delete":
                    item_id = parse_int(data.get("id", "0"), "رقم المنتج")
                    delete_item(db, item_id)
                    self.redirect(f"/products?msg={arabic_query('تم حذف المنتج')}")
                    return
                if parsed.path == "/cart/add":
                    item_id = parse_int(data.get("id", "0"), "رقم المنتج")
                    qty = parse_int(data.get("qty", "1"), "الكمية")
                    item = get_item(db, item_id)
                    if not item:
                        raise ValueError("المنتج غير موجود")
                    current_qty = CARTS.setdefault(sid, {}).get(item_id, 0)
                    if current_qty + qty > int(item["stock"]):
                        raise ValueError("الكمية المطلوبة أكبر من المخزون")
                    CARTS[sid][item_id] = current_qty + qty
                    self.redirect(f"/pos?msg={arabic_query('تمت إضافة المنتج إلى السلة')}")
                    return
                if parsed.path == "/cart/remove":
                    item_id = parse_int(data.get("id", "0"), "رقم المنتج")
                    CARTS.setdefault(sid, {}).pop(item_id, None)
                    self.redirect(f"/pos?msg={arabic_query('تم حذف المنتج من السلة')}")
                    return
                if parsed.path == "/cart/clear":
                    CARTS[sid] = {}
                    self.redirect(f"/pos?msg={arabic_query('تم تفريغ السلة')}")
                    return
                if parsed.path == "/checkout":
                    paid = parse_float(data.get("paid", "0"), "المبلغ المدفوع")
                    payment_type = data.get("payment_type", "cash").strip()
                    customer_name = data.get("customer_name", "").strip()
                    sale_id, total, change = create_sale(
                        db,
                        CARTS.setdefault(sid, {}),
                        paid,
                        payment_type,
                        customer_name,
                    )
                    CARTS[sid] = {}
                    if payment_type == "debt":
                        debt_amount = total - paid
                        message = f"تم تسجيل بيع بالدين رقم {sale_id} - الزبون {customer_name} - الدين {money(debt_amount)}"
                    else:
                        message = f"تمت عملية البيع رقم {sale_id} - الإجمالي {money(total)} - الباقي {money(change)}"
                    target = "/sales" if user_can_access(db, username, "sales") else "/pos"
                    self.redirect(f"{target}?msg={arabic_query(message)}")
                    return
                if parsed.path == "/users/create":
                    user_data = validate_user_form(data)
                    create_user(db, *user_data)
                    self.redirect(f"/users?msg={arabic_query('تمت إضافة المستخدم بنجاح')}")
                    return
                if parsed.path == "/users/update":
                    user_id = parse_int(data.get("id", "0"), "رقم المستخدم")
                    if not get_user(db, user_id):
                        raise ValueError("المستخدم غير موجود")
                    user_data = validate_user_form(data, require_password=False)
                    update_user(db, user_id, *user_data)
                    self.redirect(f"/users?msg={arabic_query('تم تحديث المستخدم')}")
                    return
                if parsed.path == "/users/delete":
                    user_id = parse_int(data.get("id", "0"), "رقم المستخدم")
                    deleted_username = delete_user(db, user_id)
                    for session_id, session_username in list(SESSION_USERS.items()):
                        if session_username == deleted_username:
                            SESSION_USERS.pop(session_id, None)
                            AUTHENTICATED_SESSIONS.discard(session_id)
                            CARTS.pop(session_id, None)
                    self.redirect(f"/users?msg={arabic_query('تم حذف المستخدم')}")
                    return
        except ValueError as exc:
            if parsed.path.startswith("/users"):
                target = "/users"
            elif parsed.path.startswith(("/cart", "/checkout")):
                target = "/pos"
            else:
                target = "/products"
            self.redirect(f"{target}?err={arabic_query(str(exc))}")
            return

        self.respond_text("الصفحة غير موجودة", HTTPStatus.NOT_FOUND)

    def is_authenticated(self, sid: str) -> bool:
        return sid in AUTHENTICATED_SESSIONS and sid in SESSION_USERS

    def current_username(self, sid: str) -> str:
        return SESSION_USERS.get(sid, "")

    def authenticate(self, db: sqlite3.Connection, username: str, password: str) -> bool:
        if secure_text_equal(username, LOGIN_USERNAME) and secure_text_equal(password, LOGIN_PASSWORD):
            return True
        user = get_user_by_username(db, username)
        if not user or not int(user["is_active"]):
            return False
        return secure_text_equal(password, str(user["password"]))

    def ensure_session(self) -> str:
        cookie = self.headers.get("Cookie", "")
        sid = ""
        for chunk in cookie.split(";"):
            name, _, value = chunk.strip().partition("=")
            if name == "cashier_sid":
                sid = value
                break
        if not sid:
            sid = secrets.token_urlsafe(16)
            self.new_sid = sid
        else:
            self.new_sid = ""
        CARTS.setdefault(sid, {})
        return sid

    def read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        parsed = parse_qs(body, keep_blank_values=True)
        return {key: values[0] for key, values in parsed.items()}

    def send_base_headers(self, status: HTTPStatus, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if getattr(self, "new_sid", ""):
            self.send_header("Set-Cookie", f"cashier_sid={self.new_sid}; Path=/; HttpOnly; SameSite=Lax")

    def respond_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = body.encode("utf-8")
        self.send_base_headers(status, "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def respond_text(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = body.encode("utf-8")
        self.send_base_headers(status, "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def redirect(self, target: str) -> None:
        self.send_base_headers(HTTPStatus.SEE_OTHER, "text/plain; charset=utf-8")
        self.send_header("Location", target)
        self.end_headers()

    def serve_static(self, request_path: str) -> None:
        file_name = request_path.removeprefix("/static/")
        safe_path = (STATIC_DIR / file_name).resolve()
        if not str(safe_path).startswith(str(STATIC_DIR.resolve())) or not safe_path.exists():
            self.respond_text("الملف غير موجود", HTTPStatus.NOT_FOUND)
            return
        content_type = "text/css; charset=utf-8" if safe_path.suffix == ".css" else "application/octet-stream"
        payload = safe_path.read_bytes()
        self.send_base_headers(HTTPStatus.OK, content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{now_text()}] {self.address_string()} - {format % args}")


def main() -> None:
    init_db()
    seed_demo_items()
    server = ThreadingHTTPServer((HOST, PORT), CashierHandler)
    print(json.dumps({"url": f"http://{PUBLIC_HOST}:{PORT}", "listen": f"{HOST}:{PORT}", "database": str(DB_PATH)}, ensure_ascii=False))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nتم إيقاف النظام.")


if __name__ == "__main__":
    main()

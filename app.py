import os
import io
import re
import json
import base64
import secrets
import sqlite3
import hmac
import hashlib
import requests
import importlib
from datetime import datetime, timezone, timedelta
from functools import wraps
from urllib.parse import urlencode, quote_plus
from dotenv import load_dotenv
from flask import (
    Flask, render_template_string, request, jsonify, Response,
    stream_with_context, session, redirect, url_for, flash, g
)
from werkzeug.security import generate_password_hash, check_password_hash
from pypdf import PdfReader

# OAuth (đăng nhập bằng Google) — dùng Authlib, cài qua requirements.txt
try:
    oauth_module = importlib.import_module("authlib.integrations.flask_client")
    OAuth = getattr(oauth_module, "OAuth", None)
except ImportError:
    OAuth = None

# Optional dependencies — the app still runs without them, just with reduced features.
try:
    docx_lib = importlib.import_module("docx")
except ImportError:
    docx_lib = None

try:
    from PIL import Image
except ImportError:
    Image = None

# ==========================================
# 0. CẤU HÌNH ỨNG DỤNG
# ==========================================
load_dotenv()  # đọc các biến từ file .env cùng thư mục (nếu có)

app = Flask(__name__)
# Trần cứng ở tầng Flask/Werkzeug — áp dụng cho MỌI request trước khi code của ta kịp chạy.
# Đặt bằng đúng mức trần cao nhất trong số các gói (Max = 1GB/file); giới hạn thấp hơn cho
# từng gói (Free/Premium) được kiểm tra riêng, chi tiết hơn, ngay trong route /api/upload.
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 1024 + 8 * 1024 * 1024  # 1GB + đệm cho overhead multipart
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'


@app.template_filter('vnd')
def _format_vnd(amount):
    """Format số tiền kiểu Việt Nam: dấu chấm ngăn cách hàng nghìn (vd: 30000 -> '30.000')."""
    try:
        return f"{int(amount):,}".replace(',', '.')
    except (TypeError, ValueError):
        return str(amount)


from werkzeug.middleware.proxy_fix import ProxyFix

# Render / Nginx: tin 1 lớp proxy (HTTPS, Host)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Khóa bí mật để ký session cookie (đăng nhập). Nên đặt cố định qua biến môi trường
# SECRET_KEY trong .env khi deploy thật, nếu không mỗi lần restart server người dùng
# sẽ bị đăng xuất (vì khóa được sinh ngẫu nhiên lại).
_env_secret = os.environ.get("SECRET_KEY", "").strip()
if _env_secret:
    app.secret_key = _env_secret
else:
    app.secret_key = secrets.token_hex(32)
    print("⚠️  CẢNH BÁO: Chưa có SECRET_KEY trong .env — phiên đăng nhập sẽ mất khi restart server.")
    print("   Thêm dòng sau vào .env để cố định: SECRET_KEY=" + secrets.token_hex(16))

# Giới hạn số ký tự trích từ file để tránh vượt quá token limit khi gửi cho AI (mức Free —
# Premium/Max có mức cao hơn hoặc không giới hạn, xem PLAN_LIMITS bên dưới).
MAX_FILE_CHARS = 12000
MAX_IMAGE_DIMENSION = 1600  # px, ảnh lớn hơn sẽ được thu nhỏ để gửi AI nhanh hơn

ALLOWED_IMAGE_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
ALLOWED_DOC_EXT = {'.pdf', '.docx', '.txt', '.csv'}

USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{3,32}$')

# Lấy cấu hình từ biến môi trường / file .env (KHÔNG hard-code API key trong code!)
CONSOLEX_API_BASE = os.environ.get("CONSOLEX_API_BASE", "https://api.x.ai/v1")
XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
CONSOLEX_MODEL = os.environ.get("CONSOLEX_MODEL", "grok-4.5")

if not XAI_API_KEY:
    print("⚠️  CẢNH BÁO: Chưa thiết lập XAI_API_KEY.")
    print("   Tạo file .env cùng thư mục với app.py, nội dung:")
    print("   XAI_API_KEY=xai-xxxxxxxxxxxxxxxx")

# Session dùng chung để tái sử dụng kết nối TCP/TLS tới xAI -> giảm độ trễ mỗi request.
SESSION = requests.Session()

# ==========================================
# 0.05. ĐĂNG NHẬP BẰNG GOOGLE (OAuth 2.0)
# ==========================================
# Lấy Client ID / Client Secret từ .env. Nếu không đặt, nút tương ứng sẽ tự ẩn
# trên trang đăng nhập — app vẫn chạy bình thường với đăng nhập bằng mật khẩu.
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '').strip()
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '').strip()

GOOGLE_OAUTH_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and OAuth)

oauth = OAuth(app) if OAuth else None

if GOOGLE_OAUTH_ENABLED:
    oauth.register(
        name='google',
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile'},
    )

if not GOOGLE_OAUTH_ENABLED:
    print("ℹ️  Đăng nhập Google đang TẮT (chưa đặt Client ID/Secret trong .env).")
    print("   Xem hướng dẫn lấy Client ID/Secret trong README.md để bật.")

# ==========================================
# 0.1. CƠ SỞ DỮ LIỆU (Tài khoản + Lịch sử chat) — SQLite
# ==========================================
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'studymate.db')


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (conversation_id) REFERENCES conversations (id)
        )
    ''')
    # Nhật ký "lượt sử dụng" AI — mỗi lần học sinh gửi câu hỏi tới /api/chat sẽ có 1 dòng ở đây.
    # Dùng để dựng trang thống kê cho tài khoản developer (KHÔNG lưu nội dung câu hỏi/trả lời,
    # chỉ lưu số liệu tổng quát: độ dài, môn học, chế độ, có kèm file/ảnh không, trạng thái).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS usage_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            endpoint TEXT NOT NULL,
            subject TEXT,
            mode TEXT,
            message_chars INTEGER DEFAULT 0,
            response_chars INTEGER DEFAULT 0,
            had_file INTEGER DEFAULT 0,
            had_image INTEGER DEFAULT 0,
            status TEXT DEFAULT 'ok',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    # "Dự án" (giống Claude Projects) — nhóm các đoạn chat lại theo chủ đề của riêng từng tài khoản.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    # Cấu hình hệ thống dạng key-value (thông báo chung, bật/tắt tính năng...) do developer chỉnh.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    conn.commit()

    # ---- Di trú (migration) cho database cũ đã tồn tại trước khi có cột "role" ----
    existing_cols = [r[1] for r in conn.execute('PRAGMA table_info(users)').fetchall()]
    if 'role' not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
        conn.commit()

    # ---- Di trú cho đăng nhập Google (OAuth) ----
    # email: dùng để hiển thị / tránh trùng tài khoản OAuth.
    # oauth_provider + oauth_id: định danh duy nhất của tài khoản bên Google.
    # Tài khoản tạo qua OAuth sẽ có password_hash = '' (không thể đăng nhập bằng mật khẩu).
    if 'email' not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
        conn.commit()
    if 'oauth_provider' not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN oauth_provider TEXT")
        conn.commit()
    if 'oauth_id' not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN oauth_id TEXT")
        conn.commit()
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_oauth ON users (oauth_provider, oauth_id) "
        "WHERE oauth_provider IS NOT NULL"
    )
    conn.commit()

    # ---- Di trú: tuỳ chỉnh cá nhân theo tài khoản (giao diện, ngôn ngữ, môn/chế độ mặc định) ----
    if 'preferences' not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN preferences TEXT DEFAULT '{}'")
        conn.commit()

    # ---- Di trú: Ghim đoạn chat + gom vào "Dự án" (giống Claude Projects) ----
    conv_cols = [r[1] for r in conn.execute('PRAGMA table_info(conversations)').fetchall()]
    if 'pinned' not in conv_cols:
        conn.execute("ALTER TABLE conversations ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    if 'project_id' not in conv_cols:
        conn.execute("ALTER TABLE conversations ADD COLUMN project_id INTEGER")
        conn.commit()

    # ---- Di trú: hệ thống vai trò 4 cấp (user → developer → admin → super_admin) ----
    # Khoá tài khoản (is_locked/lock_reason) + "reset session" (session_version: tăng số này
    # sẽ làm mọi phiên đăng nhập cũ của tài khoản đó tự động bị đăng xuất ở lần request kế tiếp).
    existing_cols = [r[1] for r in conn.execute('PRAGMA table_info(users)').fetchall()]
    if 'is_locked' not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_locked INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    if 'lock_reason' not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN lock_reason TEXT DEFAULT ''")
        conn.commit()
    if 'session_version' not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    # ---- Di trú: gói sử dụng (Free/Premium/Max) ----
    # Chỉ áp dụng thật sự cho tài khoản role='user' — Developer trở lên luôn có Max vô điều
    # kiện, tính động qua effective_plan() ở runtime (xem mục 0.25), không đọc từ cột này.
    if 'plan' not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'")
        conn.commit()

    # Gói Premium/Max giờ tính THEO THÁNG (không phải vĩnh viễn) — plan_expires_at là mốc
    # hết hạn. Hết hạn mà chưa gia hạn thì effective_plan() tự coi như 'free' (không cần job
    # nền dọn dẹp gì cả, tính "lazy" ngay lúc đọc — xem effective_plan() ở mục 0.25).
    if 'plan_expires_at' not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN plan_expires_at TEXT")
        conn.commit()

    # "AI Tutor" tuỳ chỉnh do developer trở lên tự tạo (tên + system prompt riêng).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS custom_tutors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            system_prompt TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (owner_id) REFERENCES users (id)
        )
    ''')
    # API Key cho developer trở lên — chỉ lưu bản băm (hash), không bao giờ lưu key gốc.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            key_prefix TEXT NOT NULL,
            key_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            revoked INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    # Nhật ký thao tác nhạy cảm (đổi vai trò, khoá tài khoản, xoá tài khoản, cấu hình hệ thống...)
    # — chỉ Super Admin xem được, phục vụ truy vết trách nhiệm (audit trail).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id INTEGER,
            actor_username TEXT,
            action TEXT NOT NULL,
            target TEXT DEFAULT '',
            detail TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    ''')
    # Nhật ký từng lượt tải file/ảnh lên — dùng để tính giới hạn "X file/ảnh mỗi 24h" theo
    # gói (Free/Premium/Max). Đếm theo cửa sổ trượt 24h kể từ thời điểm hỏi (rolling window),
    # KHÔNG reset cứng theo nửa đêm — đúng như yêu cầu "thời gian reset 24h".
    conn.execute('''
        CREATE TABLE IF NOT EXISTS file_uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            size_bytes INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    # Đơn nâng cấp gói (thanh toán). "method" = 'vnpay' (ATM/Visa/Mastercard/JCB qua cổng
    # VNPAY) hoặc 'bank_transfer' (chuyển khoản quét mã VietQR, xác nhận thủ công bởi Admin).
    # "order_code" vừa là mã tra cứu, vừa dùng làm vnp_TxnRef / nội dung chuyển khoản.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS payment_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_code TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            plan TEXT NOT NULL,
            amount INTEGER NOT NULL,
            base_amount INTEGER NOT NULL DEFAULT 0,
            is_discounted INTEGER NOT NULL DEFAULT 0,
            method TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            provider_txn_id TEXT,
            created_at TEXT NOT NULL,
            paid_at TEXT,
            note TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    payment_cols = [r[1] for r in conn.execute('PRAGMA table_info(payment_orders)').fetchall()]
    if 'base_amount' not in payment_cols:
        conn.execute("ALTER TABLE payment_orders ADD COLUMN base_amount INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    if 'is_discounted' not in payment_cols:
        conn.execute("ALTER TABLE payment_orders ADD COLUMN is_discounted INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    # "Bộ nhớ" AI — điều đáng nhớ về 1 học sinh (môn yếu, mục tiêu, cách giải thích ưa thích...)
    # để cá nhân hoá câu trả lời ở các lượt chat sau. category giúp phân loại khi hiển thị.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            source TEXT NOT NULL DEFAULT 'auto',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    # Báo lỗi câu trả lời từ học sinh — gắn với 1 đoạn chat cụ thể (nếu có) để Admin xem lại.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS issue_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            conversation_id INTEGER,
            message_excerpt TEXT,
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            resolved_by TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    # Gamification nhẹ: XP + streak (số ngày học liên tiếp) theo tài khoản.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS user_stats (
            user_id INTEGER PRIMARY KEY,
            xp INTEGER NOT NULL DEFAULT 0,
            streak_days INTEGER NOT NULL DEFAULT 0,
            longest_streak INTEGER NOT NULL DEFAULT 0,
            last_active_date TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS achievements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            earned_at TEXT NOT NULL,
            UNIQUE(user_id, code),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.commit()

    # ---- Tạo (hoặc nâng cấp) tài khoản developer ----
    # Có thể tuỳ chỉnh qua .env: DEVELOPER_USERNAME / DEVELOPER_PASSWORD.
    # Nếu chưa có tài khoản này, server sẽ tự tạo và in mật khẩu ra console 1 lần duy nhất.
    dev_username = (os.environ.get('DEVELOPER_USERNAME', '') or 'developer').strip()
    dev_row = conn.execute('SELECT id, role FROM users WHERE username = ?', (dev_username,)).fetchone()
    if dev_row:
        if dev_row[1] != 'developer':
            conn.execute("UPDATE users SET role = 'developer' WHERE id = ?", (dev_row[0],))
            conn.commit()
            print(f"👨‍💻 Đã nâng quyền tài khoản '{dev_username}' thành developer.")
    else:
        dev_password = os.environ.get('DEVELOPER_PASSWORD', '').strip()
        auto_generated = False
        if not dev_password:
            dev_password = secrets.token_urlsafe(9)
            auto_generated = True
        conn.execute(
            'INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)',
            (dev_username, generate_password_hash(dev_password), 'developer', now_iso())
        )
        conn.commit()
        print("👨‍💻 Đã tạo tài khoản developer mới:")
        print(f"   Tên đăng nhập: {dev_username}")
        if auto_generated:
            print(f"   Mật khẩu (tự sinh — hãy đăng nhập và đổi ngay): {dev_password}")
            print("   Gợi ý: đặt DEVELOPER_USERNAME / DEVELOPER_PASSWORD trong .env để cố định thông tin này.")
        else:
            print("   Mật khẩu: lấy từ DEVELOPER_PASSWORD trong .env")

    # ---- Nâng tài khoản chỉ định thành Super Admin ----
    # Super Admin đứng trên cùng hệ thống phân quyền (bao hàm mọi quyền của Admin/Developer/User).
    # Tên tài khoản lấy từ .env (SUPER_ADMIN_USERNAME) — mặc định "BlackadaNutella" theo yêu cầu.
    # Chỉ áp dụng nếu tài khoản đã tồn tại sẵn — không tự tạo tài khoản mới ở đây vì không có
    # mật khẩu do người dùng đặt để gán vào.
    super_admin_username = (os.environ.get('SUPER_ADMIN_USERNAME', '') or 'BlackadaNutella').strip()
    sa_row = conn.execute('SELECT id, role FROM users WHERE username = ?', (super_admin_username,)).fetchone()
    if sa_row and sa_row[1] != 'super_admin':
        conn.execute("UPDATE users SET role = 'super_admin' WHERE id = ?", (sa_row[0],))
        conn.commit()
        print(f"👑 Đã nâng tài khoản '{super_admin_username}' thành Super Admin (có toàn bộ quyền, kể cả Developer).")

    conn.close()


def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_db(_exc):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ==========================================
# 0.2. HỆ THỐNG VAI TRÒ (4 cấp, mỗi cấp bao hàm quyền của cấp dưới)
# ==========================================
# user < developer < admin < super_admin.
# Super Admin có TẤT CẢ quyền của Admin, Admin có TẤT CẢ quyền của Developer, v.v.
# (không cần gán nhiều vai trò cùng lúc — 1 cột "role" duy nhất, cấp cao hơn tự động
# thừa hưởng quyền của cấp thấp hơn thông qua role_rank()).
ROLE_ORDER = ['user', 'developer', 'admin', 'super_admin']
ROLE_META = {
    'user':        {'label': 'Người dùng', 'icon': '🧑‍🎓',
                     'badge': 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400'},
    'developer':   {'label': 'Developer',  'icon': '🧑‍💻',
                     'badge': 'bg-indigo-100 text-indigo-600 dark:bg-indigo-900 dark:text-indigo-300'},
    'admin':       {'label': 'Admin',      'icon': '👑',
                     'badge': 'bg-amber-100 text-amber-700 dark:bg-amber-900 dark:text-amber-300'},
    'super_admin': {'label': 'Super Admin', 'icon': '🔥',
                     'badge': 'bg-red-100 text-red-600 dark:bg-red-900 dark:text-red-300'},
}


def role_rank(role):
    try:
        return ROLE_ORDER.index(role)
    except ValueError:
        return 0


def role_meta(role):
    return ROLE_META.get(role, ROLE_META['user'])


def can_manage_role(actor_role, target_role, new_role):
    """Quy tắc: chỉ Super Admin được đụng tới vai trò Admin/Super Admin (cấp hoặc thu hồi).
    Admin chỉ được đổi qua lại giữa User <-> Developer. Không ai được hạ quyền Super Admin
    qua giao diện này (an toàn hệ thống — phải sửa trực tiếp trong DB nếu thực sự cần)."""
    if new_role not in ROLE_ORDER:
        return False, "Vai trò không hợp lệ."
    if target_role == 'super_admin' and new_role != 'super_admin':
        return False, "Không thể hạ quyền Super Admin qua giao diện này."
    if actor_role != 'super_admin':
        if role_rank(target_role) >= role_rank('admin'):
            return False, "Chỉ Super Admin mới có thể thay đổi vai trò của Admin/Super Admin."
        if role_rank(new_role) >= role_rank('admin'):
            return False, "Chỉ Super Admin mới có thể cấp quyền Admin trở lên."
    return True, None


def write_audit(action, target='', detail=''):
    """Ghi log các thao tác nhạy cảm (đổi vai trò, khoá tài khoản, cấu hình hệ thống...).
    Chỉ Super Admin xem được (trang /developer/audit)."""
    try:
        db = get_db()
        actor = current_user()
        db.execute(
            'INSERT INTO audit_logs (actor_id, actor_username, action, target, detail, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (actor['id'] if actor else None, actor['username'] if actor else 'system',
             action, target, detail, now_iso())
        )
        db.commit()
    except Exception:
        pass


# ==========================================
# 0.3. XÁC THỰC NGƯỜI DÙNG (session-based)
# ==========================================
def current_user():
    """Nạp thông tin tài khoản hiện tại từ DB 1 lần / request (cache trong g)."""
    if not hasattr(g, '_current_user'):
        uid = session.get('user_id')
        g._current_user = None
        if uid:
            g._current_user = get_db().execute('SELECT * FROM users WHERE id = ?', (uid,)).fetchone()
    return g._current_user


def current_user_id():
    return session.get('user_id')


def current_user_role():
    u = current_user()
    return u['role'] if u else 'user'


def _auth_gate(min_role=None):
    """Kiểm tra: đã đăng nhập? tài khoản có bị khoá? phiên có bị admin reset không?
    có đủ cấp vai trò tối thiểu không? Trả về response lỗi nếu vi phạm, None nếu hợp lệ."""
    if not session.get('user_id'):
        if request.path.startswith('/api/'):
            return jsonify({"error": "Vui lòng đăng nhập để tiếp tục."}), 401
        return redirect(url_for('login_page', next=request.path))

    user = current_user()
    if not user:
        session.clear()
        if request.path.startswith('/api/'):
            return jsonify({"error": "Phiên đăng nhập không hợp lệ."}), 401
        return redirect(url_for('login_page'))

    if user['is_locked']:
        session.clear()
        if request.path.startswith('/api/'):
            return jsonify({"error": "Tài khoản của bạn đã bị khoá."}), 403
        flash('Tài khoản của em đã bị khoá. Liên hệ quản trị viên nếu có thắc mắc.')
        return redirect(url_for('login_page'))

    if session.get('session_version') != user['session_version']:
        session.clear()
        if request.path.startswith('/api/'):
            return jsonify({"error": "Phiên đăng nhập đã được đặt lại. Vui lòng đăng nhập lại."}), 401
        flash('Phiên đăng nhập của em đã được đặt lại, vui lòng đăng nhập lại.')
        return redirect(url_for('login_page'))

    if min_role and role_rank(user['role']) < role_rank(min_role):
        if request.path.startswith('/api/'):
            return jsonify({"error": "Bạn không có quyền truy cập chức năng này."}), 403
        flash('Tài khoản của em không có quyền truy cập trang này.')
        return redirect(url_for('home'))

    return None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        err = _auth_gate()
        return err if err is not None else view(*args, **kwargs)
    return wrapped


def developer_required(view):
    """Từ Developer trở lên (Developer / Admin / Super Admin)."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        err = _auth_gate(min_role='developer')
        return err if err is not None else view(*args, **kwargs)
    return wrapped


def admin_required(view):
    """Từ Admin trở lên (Admin / Super Admin)."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        err = _auth_gate(min_role='admin')
        return err if err is not None else view(*args, **kwargs)
    return wrapped


def super_admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        err = _auth_gate(min_role='super_admin')
        return err if err is not None else view(*args, **kwargs)
    return wrapped


# ==========================================
# 0.25. GÓI SỬ DỤNG (Free / Premium / Max) & CHẾ ĐỘ SUY NGHĨ AI
# ==========================================
# free < premium < max — mỗi gói cao hơn kế thừa toàn bộ quyền lợi của gói thấp hơn.
# Developer/Admin/Super Admin luôn được cấp Max VÔ ĐIỀU KIỆN (tính động qua effective_plan(),
# không cần ghi vào cột `plan` trong DB) — đây là quyền lợi đi kèm vai trò, không phải trả phí.
PLAN_ORDER = ['free', 'premium', 'max']
PLAN_META = {
    'free':    {'label': 'Free',    'icon': '🆓',
                'badge': 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400'},
    'premium': {'label': 'Premium', 'icon': '💎',
                'badge': 'bg-indigo-100 text-indigo-600 dark:bg-indigo-900 dark:text-indigo-300'},
    'max':     {'label': 'Max',     'icon': '🚀',
                'badge': 'bg-amber-100 text-amber-700 dark:bg-amber-900 dark:text-amber-300'},
}
# daily_uploads: số file/ảnh tối đa trong 24h gần nhất (rolling window, không phải theo lịch) —
# None = không giới hạn. max_file_mb: dung lượng tối đa MỖI file/ảnh. text_chars: số ký tự tối
# đa trích xuất từ file văn bản/PDF/Word trước khi bị cắt bớt — None = không cắt.
PLAN_LIMITS = {
    'free':    {'daily_uploads': 20,   'max_file_mb': 20,   'text_chars': MAX_FILE_CHARS},
    'premium': {'daily_uploads': 50,   'max_file_mb': 500,  'text_chars': MAX_FILE_CHARS * 4},
    'max':     {'daily_uploads': None, 'max_file_mb': 1024, 'text_chars': None},
}
UPLOAD_QUOTA_WINDOW_HOURS = 24

# Giá nâng cấp gói (VNĐ/THÁNG) — Premium 30.000đ/tháng, Max 50.000đ/tháng. Đây là subscription
# THEO THÁNG: mỗi đơn thanh toán thành công chỉ cấp đúng 1 THÁNG quyền lợi (xem add_one_month()
# + grant_plan_upgrade()), hết hạn tự rơi về Free nếu không thanh toán tiếp — KHÔNG tự động trừ
# tiền định kỳ (app không lưu thông tin thẻ để làm việc đó), học sinh cần tự vào lại nâng cấp
# mỗi tháng. Free không cần thanh toán nên không có trong bảng giá.
PLAN_PRICING = {
    'premium': 30000,
    'max': 50000,
}

# Ưu đãi lần đầu: 3 THÁNG ĐẦU TIÊN học sinh từng thanh toán thành công (bất kể gói Premium hay
# Max) được giảm giá; từ tháng thanh toán thứ 4 trở đi tính giá bình thường. Đếm theo TỔNG SỐ
# đơn đã thanh toán thành công trong lịch sử tài khoản (payment_orders.status='paid'), không
# phân biệt loại gói — nâng cấp/hạ cấp giữa Premium <-> Max vẫn tính chung 1 "tháng đã dùng ưu đãi".
FIRST_TIME_DISCOUNT_PCT = 50       # % giảm — có thể chỉnh lại nếu ý bạn là mức khác
FIRST_TIME_DISCOUNT_MONTHS = 3     # số THÁNG đầu được hưởng ưu đãi

# ==========================================
# 0.26. THANH TOÁN NÂNG CẤP GÓI — VNPAY (ATM/Visa/Mastercard/JCB) + Chuyển khoản VietQR
# ==========================================
# VNPAY: cổng thanh toán thẻ (ATM nội địa qua NAPAS, thẻ quốc tế Visa/Mastercard/JCB, ví
# VNPAY QR). Cần đăng ký tài khoản merchant tại https://vnpay.vn để lấy vnp_TmnCode +
# vnp_HashSecret — CHƯA đăng ký thì tính năng này tự ẩn khỏi giao diện (app vẫn chạy bình
# thường, chỉ còn phương thức Chuyển khoản VietQR bên dưới).
VNPAY_TMN_CODE = os.environ.get('VNPAY_TMN_CODE', '').strip()
VNPAY_HASH_SECRET = os.environ.get('VNPAY_HASH_SECRET', '').strip()
VNPAY_PAYMENT_URL = os.environ.get('VNPAY_PAYMENT_URL', 'https://sandbox.vnpayment.vn/paymentv2/vpcpay.html').strip()
VNPAY_ENABLED = bool(VNPAY_TMN_CODE and VNPAY_HASH_SECRET)
if not VNPAY_ENABLED:
    print("ℹ️  Thanh toán VNPAY (thẻ ATM/Visa/Mastercard) đang TẮT — chưa cấu hình "
          "VNPAY_TMN_CODE/VNPAY_HASH_SECRET trong .env. Xem README mục 14 để đăng ký & bật.")

# Chuyển khoản ngân hàng qua mã VietQR (img.vietqr.io — dịch vụ công khai, MIỄN PHÍ, không
# cần API key: chỉ cần đúng số tài khoản NGÂN HÀNG CỦA BẠN thì ảnh QR tạo ra mới chuyển tiền
# vào đúng chỗ). Học sinh quét bằng app ngân hàng bất kỳ, hoặc MoMo/ZaloPay (2 ví này đều hỗ
# trợ quét mã VietQR chuẩn NAPAS để chuyển thẳng vào tài khoản ngân hàng — không cần tích hợp
# API riêng của MoMo/ZaloPay). Việc xác nhận "đã nhận tiền" hiện làm THỦ CÔNG bởi Admin (bấm
# 1 nút ở /developer) vì app không có quyền đọc sao kê ngân hàng tự động.
VIETQR_BANK_ID = os.environ.get('VIETQR_BANK_ID', '').strip()       # vd: 'mbbank', 'vietinbank', hoặc mã BIN '970422'
VIETQR_ACCOUNT_NO = os.environ.get('VIETQR_ACCOUNT_NO', '').strip()
VIETQR_ACCOUNT_NAME = os.environ.get('VIETQR_ACCOUNT_NAME', '').strip()
BANK_TRANSFER_ENABLED = bool(VIETQR_BANK_ID and VIETQR_ACCOUNT_NO and VIETQR_ACCOUNT_NAME)
if not BANK_TRANSFER_ENABLED:
    print("ℹ️  Thanh toán Chuyển khoản VietQR đang TẮT — chưa cấu hình VIETQR_BANK_ID/"
          "VIETQR_ACCOUNT_NO/VIETQR_ACCOUNT_NAME trong .env. Xem README mục 14 để bật.")

PAYMENT_METHODS_ENABLED = VNPAY_ENABLED or BANK_TRANSFER_ENABLED


def generate_order_code():
    """Mã đơn hàng ngắn, duy nhất — dùng làm vnp_TxnRef (VNPAY) và nội dung chuyển khoản
    (VietQR) nên phải NGẮN, chỉ chữ+số (không dấu, không khoảng trắng) để tránh lỗi ký tự
    đặc biệt khi ngân hàng/VNPAY xử lý nội dung giao dịch."""
    return 'SM' + datetime.now(timezone.utc).strftime('%y%m%d') + secrets.token_hex(3).upper()


def add_one_month(dt):
    """Cộng đúng 1 THÁNG LỊCH (không phải 30 ngày) — vd 31/1 + 1 tháng = 28 hoặc 29/2 (tự
    kẹp về ngày cuối cùng của tháng đích nếu tháng đích ngắn hơn). Chỉ dùng thư viện chuẩn
    (datetime + calendar), không cần cài thêm dateutil."""
    import calendar
    year = dt.year + (dt.month // 12)
    month = dt.month % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    day = min(dt.day, last_day)
    return dt.replace(year=year, month=month, day=day)


def compute_checkout_price(user_id, plan):
    """Tính giá THÁNG NÀY cho 1 gói, áp dụng ưu đãi lần đầu nếu còn hạn mức (xem
    FIRST_TIME_DISCOUNT_PCT/FIRST_TIME_DISCOUNT_MONTHS). Trả về (amount, base_amount,
    is_discounted, paid_months_so_far)."""
    base_amount = PLAN_PRICING[plan]
    conn = open_write_db()
    try:
        paid_count = conn.execute(
            "SELECT COUNT(*) c FROM payment_orders WHERE user_id = ? AND status = 'paid'", (user_id,)
        ).fetchone()['c']
    finally:
        conn.close()
    is_discounted = paid_count < FIRST_TIME_DISCOUNT_MONTHS
    amount = round(base_amount * (100 - FIRST_TIME_DISCOUNT_PCT) / 100) if is_discounted else base_amount
    return amount, base_amount, is_discounted, paid_count


def vietqr_image_url(amount, order_code):
    """Trả về link ảnh QR chuyển khoản (dịch vụ công khai img.vietqr.io, không cần API key).
    Học sinh quét mã này bằng app ngân hàng hoặc MoMo/ZaloPay để chuyển thẳng vào tài khoản
    ngân hàng đã cấu hình — số tiền + nội dung chuyển khoản được điền sẵn trong mã QR."""
    from urllib.parse import quote
    return (
        f"https://img.vietqr.io/image/{quote(VIETQR_BANK_ID)}-{quote(VIETQR_ACCOUNT_NO)}-compact2.png"
        f"?amount={int(amount)}&addInfo={quote(order_code)}&accountName={quote(VIETQR_ACCOUNT_NAME)}"
    )


def vnpay_sign(params: dict) -> str:
    """Ký (hoặc xác thực) dữ liệu theo đúng thuật toán VNPAY yêu cầu: sắp xếp key theo
    alphabet, nối thành query string đã URL-encode giá trị, rồi HMAC-SHA512 với vnp_HashSecret.
    Dùng chung cho cả lúc TẠO link thanh toán lẫn lúc XÁC THỰC callback (Return URL / IPN)."""
    sorted_items = sorted(params.items())
    query_string = urlencode(sorted_items, quote_via=quote_plus)
    return hmac.new(
        VNPAY_HASH_SECRET.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha512
    ).hexdigest()


def vnpay_build_payment_url(order_code, amount, order_info, ip_addr, return_url):
    now = datetime.now(timezone(timedelta(hours=7)))  # giờ Việt Nam (ICT, UTC+7) theo yêu cầu VNPAY
    expire = now + timedelta(minutes=15)
    params = {
        'vnp_Version': '2.1.0',
        'vnp_Command': 'pay',
        'vnp_TmnCode': VNPAY_TMN_CODE,
        'vnp_Amount': str(int(amount) * 100),  # VNPAY yêu cầu nhân 100 (không thập phân)
        'vnp_CurrCode': 'VND',
        'vnp_TxnRef': order_code,
        'vnp_OrderInfo': order_info,
        'vnp_OrderType': 'other',
        'vnp_Locale': 'vn',
        'vnp_ReturnUrl': return_url,
        'vnp_IpAddr': ip_addr or '127.0.0.1',
        'vnp_CreateDate': now.strftime('%Y%m%d%H%M%S'),
        'vnp_ExpireDate': expire.strftime('%Y%m%d%H%M%S'),
    }
    secure_hash = vnpay_sign(params)
    query_string = urlencode(sorted(params.items()), quote_via=quote_plus)
    return f"{VNPAY_PAYMENT_URL}?{query_string}&vnp_SecureHash={secure_hash}"


def vnpay_verify_return(args: dict) -> bool:
    """Xác thực chữ ký vnp_SecureHash trên dữ liệu VNPAY gửi về (Return URL hoặc IPN).
    PHẢI gọi hàm này trước khi tin bất kỳ thông tin nào (mã đơn, trạng thái...) trong `args` —
    tuyệt đối không tự ý cập nhật đơn hàng thành "đã thanh toán" nếu chữ ký sai."""
    received_hash = args.get('vnp_SecureHash', '')
    check_params = {k: v for k, v in args.items() if k not in ('vnp_SecureHash', 'vnp_SecureHashType')}
    expected_hash = vnpay_sign(check_params)
    return hmac.compare_digest(received_hash, expected_hash)


def grant_plan_upgrade(user_id, plan, order_code, actor='system', months=1):
    """Gán gói THEO THÁNG cho tài khoản — hạn dùng luôn tính lại từ THỜI ĐIỂM GÁN (không cộng
    dồn vào hạn cũ nếu gia hạn sớm, để tránh rắc rối tính toán khi đổi qua lại Premium/Max).
    Gọi khi: thanh toán được xác nhận (VNPAY IPN tự động, hoặc Admin xác nhận chuyển khoản thủ
    công), hoặc Admin "tặng" 1 tháng miễn phí cho tài khoản không phải Developer trở lên (xem
    developer_change_plan()). Dùng open_write_db() vì VNPAY IPN có thể tới bất kỳ lúc nào,
    không nhất thiết trong 1 request context bình thường có sẵn `g`."""
    expires_at = add_one_month(datetime.now(timezone.utc)) if months == 1 else \
        datetime.now(timezone.utc) + timedelta(days=30 * months)
    conn = open_write_db()
    try:
        conn.execute('UPDATE users SET plan = ?, plan_expires_at = ? WHERE id = ?',
                     (plan, expires_at.isoformat(), user_id))
        conn.commit()
    finally:
        conn.close()
    write_audit('grant_plan_upgrade', target=str(user_id),
                detail=f"{plan} tới {expires_at.strftime('%d/%m/%Y')} (đơn {order_code}, xác nhận bởi {actor})")


def plan_price(plan):
    return PLAN_PRICING.get(plan)


# ==========================================
# 0.27. "BỘ NHỚ" AI — ghi nhớ cách học của từng học sinh, cá nhân hoá câu trả lời
# ==========================================
# Không gọi thêm API AI nào để trích xuất — chỉ dùng quy tắc (regex) đơn giản, nhanh và
# miễn phí. Độ chính xác vì vậy phụ thuộc vào cách học sinh diễn đạt, không phải AI tự suy
# luận/tổng hợp như một hệ thống Memory "đầy đủ" sẽ cần (xem ghi chú trong README).
MEMORY_TRIGGER_RE = re.compile(
    r'(?:ghi\s*nhớ|hãy\s*nhớ|nhớ\s*giúp|note\s*giúp|lưu\s*ý\s*giúp)(?:\s*(?:em|mình|giúp|rằng|là))*\s*[:,-]?\s*(.+)',
    re.IGNORECASE
)
GRADE_LEVEL_RE = re.compile(r'\blớp\s*(6|7|8|9|10|11|12)\b', re.IGNORECASE)
WEAK_HINT_RE = re.compile(r'yếu|kém|khó\s*hiểu|hay\s*sai|hay\s*nhầm', re.IGNORECASE)
GOAL_HINT_RE = re.compile(r'mục\s*tiêu|muốn\s*(đạt|thi|ôn)|ôn\s*thi|thi\s*vào', re.IGNORECASE)
STYLE_HINT_RE = re.compile(r'thích.*giải\s*thích|giải\s*thích.*(ngắn|dài|kỹ|đơn giản|chi tiết)', re.IGNORECASE)

MAX_MEMORY_LEN = 300
MAX_MEMORIES_IN_PROMPT = 6

MEMORY_CATEGORY_LABELS = {
    'weak_subject':     ('📉', 'Môn/chủ đề còn yếu'),
    'goal':             ('🎯', 'Mục tiêu học tập'),
    'style_preference': ('🎨', 'Cách giải thích ưa thích'),
    'topic_covered':    ('📚', 'Chủ đề đã luyện tập'),
    'general':          ('📝', 'Khác'),
}


def _guess_memory_category(text):
    if WEAK_HINT_RE.search(text):
        return 'weak_subject'
    if GOAL_HINT_RE.search(text):
        return 'goal'
    if STYLE_HINT_RE.search(text):
        return 'style_preference'
    return 'general'


def save_memory(user_id, content, category='general', source='auto'):
    """Lưu 1 mục bộ nhớ. Dùng open_write_db() vì hàm này còn được gọi từ BÊN TRONG generator
    streaming của /api/chat (xem giải thích ở docstring open_write_db())."""
    content = (content or '').strip()
    if not content:
        return
    content = content[:MAX_MEMORY_LEN]
    try:
        conn = open_write_db()
        try:
            conn.execute(
                'INSERT INTO memories (user_id, content, category, source, created_at) VALUES (?, ?, ?, ?, ?)',
                (user_id, content, category, source, now_iso())
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def extract_and_save_memory(user_id, user_message):
    """Phát hiện + lưu 1 'bộ nhớ' mới từ tin nhắn của học sinh (nếu có). Trả về nội dung vừa
    ghi nhớ (để báo lại cho học sinh biết qua 1 toast nhỏ), hoặc None nếu không có gì."""
    text = (user_message or '').strip()
    if not text:
        return None

    # 1) Học sinh chủ động yêu cầu ghi nhớ — ưu tiên cao nhất, tự đoán category theo từ khoá.
    m = MEMORY_TRIGGER_RE.search(text)
    if m:
        content = m.group(1).strip(' .!?')
        if content:
            save_memory(user_id, content, category=_guess_memory_category(content), source='explicit')
            return content

    # 2) Tự nhận diện lớp học (chỉ lưu 1 lần, tránh lặp lại mỗi khi học sinh gõ "lớp 8").
    g = GRADE_LEVEL_RE.search(text)
    if g:
        try:
            conn = open_write_db()
            try:
                existing = conn.execute(
                    "SELECT id FROM memories WHERE user_id = ? AND content LIKE 'Học sinh đang học lớp%'",
                    (user_id,)
                ).fetchone()
                if not existing:
                    content = f"Học sinh đang học lớp {g.group(1)}."
                    conn.execute(
                        'INSERT INTO memories (user_id, content, category, source, created_at) VALUES (?, ?, ?, ?, ?)',
                        (user_id, content, 'general', 'auto', now_iso())
                    )
                    conn.commit()
                    return content
            finally:
                conn.close()
        except Exception:
            pass

    return None


def track_topic_practice(user_id, subject, mode):
    """Tín hiệu 'chủ đề đã luyện tập' đơn giản: nếu học sinh làm 'Kiểm tra bài làm' từ 3 lần
    trở lên ở cùng 1 môn, tự ghi 1 mục bộ nhớ. KHÔNG suy diễn lỗi sai cụ thể là gì (muốn làm
    được vậy cần AI phân tích riêng câu trả lời — xem mục "Mistake Book" trong README)."""
    if mode != 'Kiểm tra bài làm' or not subject:
        return
    try:
        conn = open_write_db()
        try:
            marker = f"Học sinh luyện tập nhiều bài kiểm tra môn {subject}."
            existing = conn.execute(
                'SELECT id FROM memories WHERE user_id = ? AND content = ?', (user_id, marker)
            ).fetchone()
            if existing:
                return
            count_row = conn.execute(
                "SELECT COUNT(*) c FROM usage_logs WHERE user_id = ? AND subject = ? AND mode = 'Kiểm tra bài làm'",
                (user_id, subject)
            ).fetchone()
            if count_row['c'] >= 3:
                conn.execute(
                    'INSERT INTO memories (user_id, content, category, source, created_at) VALUES (?, ?, ?, ?, ?)',
                    (user_id, marker, 'topic_covered', 'auto', now_iso())
                )
                conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def get_recent_memories(user_id, limit=MAX_MEMORIES_IN_PROMPT):
    conn = open_write_db()
    try:
        rows = conn.execute(
            'SELECT content FROM memories WHERE user_id = ? ORDER BY created_at DESC LIMIT ?',
            (user_id, limit)
        ).fetchall()
        return [r['content'] for r in reversed(rows)]  # cũ -> mới, đọc tự nhiên hơn trong prompt
    finally:
        conn.close()


# ==========================================
# 0.28. GAMIFICATION NHẸ — XP + Streak (chuỗi ngày học liên tiếp) + Thành tựu
# ==========================================
XP_PER_TURN = 10
XP_PER_LEVEL = 100
ACHIEVEMENTS_META = {
    'first_lesson':  {'icon': '🧠', 'label': 'Bài học đầu tiên', 'desc': 'Hoàn thành lượt hỏi AI đầu tiên.'},
    'streak_7':      {'icon': '🔥', 'label': 'Chuỗi 7 ngày', 'desc': 'Học liên tục 7 ngày không nghỉ.'},
    'streak_30':     {'icon': '🏆', 'label': 'Chuỗi 30 ngày', 'desc': 'Học liên tục 30 ngày không nghỉ.'},
    'questions_100': {'icon': '📚', 'label': '100 câu hỏi', 'desc': 'Đã hỏi AI 100 lượt.'},
}


def award_xp_and_streak(user_id):
    """Cộng XP + cập nhật streak sau 1 lượt chat THÀNH CÔNG. Gọi từ bên trong generator
    streaming của /api/chat nên dùng open_write_db() (xem docstring open_write_db()). Trả về
    dict mô tả những gì vừa xảy ra (lên cấp? thành tựu mới?) để báo ngay trên giao diện."""
    result = {'leveled_up': False, 'new_achievements': [], 'streak_days': 0, 'xp': 0, 'level': 1}
    try:
        conn = open_write_db()
        try:
            today = datetime.now(timezone(timedelta(hours=7))).strftime('%Y-%m-%d')
            row = conn.execute('SELECT * FROM user_stats WHERE user_id = ?', (user_id,)).fetchone()
            if row is None:
                conn.execute(
                    'INSERT INTO user_stats (user_id, xp, streak_days, longest_streak, last_active_date) '
                    'VALUES (?, 0, 0, 0, NULL)', (user_id,)
                )
                conn.commit()
                row = conn.execute('SELECT * FROM user_stats WHERE user_id = ?', (user_id,)).fetchone()

            old_level = row['xp'] // XP_PER_LEVEL + 1
            new_xp = row['xp'] + XP_PER_TURN

            last_active = row['last_active_date']
            streak = row['streak_days']
            if last_active == today:
                pass  # đã hoạt động hôm nay rồi — không tăng streak thêm lần nữa
            elif last_active is None:
                streak = 1
            else:
                try:
                    last_date = datetime.strptime(last_active, '%Y-%m-%d').date()
                    today_date = datetime.strptime(today, '%Y-%m-%d').date()
                    gap = (today_date - last_date).days
                    streak = streak + 1 if gap == 1 else 1
                except ValueError:
                    streak = 1
            longest = max(row['longest_streak'], streak)

            conn.execute(
                'UPDATE user_stats SET xp = ?, streak_days = ?, longest_streak = ?, last_active_date = ? WHERE user_id = ?',
                (new_xp, streak, longest, today, user_id)
            )
            conn.commit()

            new_level = new_xp // XP_PER_LEVEL + 1
            result.update({'streak_days': streak, 'xp': new_xp, 'level': new_level, 'leveled_up': new_level > old_level})

            earned_codes = {r['code'] for r in conn.execute(
                'SELECT code FROM achievements WHERE user_id = ?', (user_id,)).fetchall()}
            total_turns = conn.execute(
                "SELECT COUNT(*) c FROM usage_logs WHERE user_id = ? AND status = 'ok'", (user_id,)
            ).fetchone()['c']

            to_check = []
            if 'first_lesson' not in earned_codes and total_turns >= 1:
                to_check.append('first_lesson')
            if 'streak_7' not in earned_codes and streak >= 7:
                to_check.append('streak_7')
            if 'streak_30' not in earned_codes and streak >= 30:
                to_check.append('streak_30')
            if 'questions_100' not in earned_codes and total_turns >= 100:
                to_check.append('questions_100')

            for code in to_check:
                try:
                    conn.execute('INSERT INTO achievements (user_id, code, earned_at) VALUES (?, ?, ?)',
                                 (user_id, code, now_iso()))
                    conn.commit()
                    result['new_achievements'].append(code)
                except sqlite3.IntegrityError:
                    pass  # trùng UNIQUE(user_id, code) — hiếm gặp, bỏ qua an toàn
        finally:
            conn.close()
    except Exception:
        pass
    return result


def get_user_stats(user_id):
    conn = open_write_db()
    try:
        row = conn.execute('SELECT * FROM user_stats WHERE user_id = ?', (user_id,)).fetchone()
        achievements_rows = conn.execute(
            'SELECT code, earned_at FROM achievements WHERE user_id = ? ORDER BY earned_at ASC', (user_id,)
        ).fetchall()
        xp = row['xp'] if row else 0
        level = xp // XP_PER_LEVEL + 1
        return {
            'xp': xp,
            'level': level,
            'xp_into_level': xp % XP_PER_LEVEL,
            'xp_per_level': XP_PER_LEVEL,
            'streak_days': row['streak_days'] if row else 0,
            'longest_streak': row['longest_streak'] if row else 0,
            'achievements': [
                {'code': a['code'], **ACHIEVEMENTS_META.get(a['code'], {'icon': '🏅', 'label': a['code'], 'desc': ''}),
                 'earned_at': a['earned_at']}
                for a in achievements_rows
            ],
        }
    finally:
        conn.close()


def plan_rank(plan):
    try:
        return PLAN_ORDER.index(plan)
    except ValueError:
        return 0


def plan_meta(plan):
    return PLAN_META.get(plan, PLAN_META['free'])


def plan_limits(plan):
    return PLAN_LIMITS.get(plan, PLAN_LIMITS['free'])


def effective_plan(user):
    """Gói THỰC TẾ đang áp dụng cho tài khoản. Developer trở lên luôn là 'max' bất kể cột
    `plan` lưu gì trong DB. Với tài khoản user thường: Premium/Max chỉ có hiệu lực nếu
    `plan_expires_at` còn hạn (gói tính THEO THÁNG — xem grant_plan_upgrade()); hết hạn thì
    tự động coi như 'free' ngay khi đọc (tính "lazy", không cần job nền dọn dẹp DB — cột
    `plan` trong DB có thể tạm thời vẫn còn ghi 'premium'/'max' cũ, nhưng hàm này luôn trả về
    giá trị ĐÚNG THỜI ĐIỂM HIỆN TẠI)."""
    if not user:
        return 'free'
    try:
        if role_rank(user['role']) >= role_rank('developer'):
            return 'max'
    except Exception:
        pass
    try:
        plan = user['plan']
        expires_at = user['plan_expires_at']
    except Exception:
        plan, expires_at = None, None
    if plan not in PLAN_ORDER or plan == 'free':
        return 'free'
    if not expires_at:
        return 'free'  # gói trả phí PHẢI có hạn sử dụng — không có hạn coi như đã hết hạn
    try:
        exp_dt = datetime.fromisoformat(expires_at)
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        if exp_dt > datetime.now(timezone.utc):
            return plan
    except ValueError:
        pass
    return 'free'


def current_effective_plan():
    return effective_plan(current_user())


def app_display_name(user):
    """Tên hiển thị của app, gắn theo gói thực tế của tài khoản đang đăng nhập — tài khoản
    Free thấy tên trơn "StudyMate AI" (không còn chữ "Pro"); Premium/Max thấy tên có gắn gói."""
    plan = effective_plan(user)
    if plan == 'max':
        return 'StudyMate AI Max'
    if plan == 'premium':
        return 'StudyMate AI Premium'
    return 'StudyMate AI'


# "Chế độ suy nghĩ" — các mức độ suy luận sâu/rộng khác nhau mà AI sẽ áp dụng khi trả lời.
# Đặt tên theo hành trình một học sinh "lên trình": Trợ Lý (mặc định, mọi gói) → Học Giả /
# Giáo Sư (mở khoá từ Premium) → Thiên Tài (độc quyền Max — mạnh nhất, kết hợp cả hai).
THINKING_MODE_ORDER = ['standard', 'scholar', 'professor', 'genius']
THINKING_MODES = {
    'standard': {
        'key': 'standard', 'label': 'Trợ Lý', 'icon': '💬', 'min_plan': 'free', 'max_tokens': 800,
        'desc': 'Phản hồi nhanh, cân bằng — phù hợp phần lớn câu hỏi hằng ngày.',
        'prompt_hint': '',
    },
    'scholar': {
        'key': 'scholar', 'label': 'Học Giả', 'icon': '📖', 'min_plan': 'premium', 'max_tokens': 1400,
        'desc': 'Suy luận từng bước kỹ càng hơn trước khi trả lời — hợp bài khó, cần độ chính xác cao.',
        'prompt_hint': ('Hãy suy nghĩ cẩn thận, từng bước một trong đầu, kiểm tra lại logic trước khi '
                         'đưa ra câu trả lời cuối cùng. Trình bày ngắn gọn các bước suy luận chính, '
                         'sau đó kết luận thật rõ ràng.'),
    },
    'professor': {
        'key': 'professor', 'label': 'Giáo Sư', 'icon': '🎓', 'min_plan': 'premium', 'max_tokens': 1600,
        'desc': 'Giải thích mở rộng hơn — nhiều ví dụ, liên hệ thực tế, nhiều góc nhìn.',
        'prompt_hint': ('Hãy giải thích mở rộng và sâu hơn bình thường: thêm ví dụ minh hoạ, liên hệ '
                         'thực tế, so sánh nhiều cách tiếp cận nếu phù hợp — giúp học sinh hiểu bản '
                         'chất chứ không chỉ nhớ đáp số.'),
    },
    'genius': {
        'key': 'genius', 'label': 'Thiên Tài', 'icon': '🌟', 'min_plan': 'max', 'max_tokens': 2200,
        'desc': 'Kết hợp suy luận sâu nhất + mở rộng kiến thức tối đa — chế độ mạnh nhất, độc quyền Max.',
        'prompt_hint': ('Hãy kết hợp cả hai: suy luận từng bước thật kỹ để đảm bảo chính xác tuyệt đối, '
                         'đồng thời giải thích mở rộng với ví dụ, liên hệ thực tế và nhiều góc nhìn. Đây '
                         'là chế độ mạnh nhất — hãy đầu tư chất lượng tối đa cho câu trả lời.'),
    },
}


def thinking_mode_unlocked(mode_key, plan):
    tm = THINKING_MODES.get(mode_key)
    if not tm:
        return False
    return plan_rank(plan) >= plan_rank(tm['min_plan'])


def resolve_thinking_mode(requested_key, plan):
    """Trả về key hợp lệ: nếu chế độ yêu cầu không tồn tại hoặc vượt quá gói hiện tại
    (kể cả khi client cố tình gửi thẳng key bị khoá qua API) thì rơi về 'standard'."""
    if requested_key in THINKING_MODES and thinking_mode_unlocked(requested_key, plan):
        return requested_key
    return 'standard'


def open_write_db():
    """Kết nối SQLite RIÊNG, không qua `g`/`get_db()`.

    Lý do cần cái này: `stream_with_context` (dùng cho các response dạng SSE streaming,
    xem `chat()` bên dưới) chỉ "hoãn" được request/session/g ở mức tham chiếu Python —
    nó KHÔNG ngăn được `teardown_appcontext` (hàm `close_db`) chạy sớm hơn generator.
    Trong Flask hiện tại, `ctx.pop()` ở cuối `wsgi_app()` (đóng kết nối `g._database`
    qua `close_db`) xảy ra NGAY khi view function return Response — tức là TRƯỚC khi
    generator SSE thật sự bắt đầu chạy và stream dữ liệu. Nếu generator dùng lại
    `get_db()`/`g._database` để ghi DB, nó sẽ gặp lỗi "Cannot operate on a closed
    database" vì kết nối đó đã bị `close_db` đóng mất rồi.
    Giải pháp: bất kỳ đoạn code nào ghi DB BÊN TRONG một generator streaming (SSE) đều
    phải tự mở kết nối riêng bằng hàm này, dùng xong tự đóng — không phụ thuộc vào `g`."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def log_usage(user_id, subject, mode, message_chars, response_chars, had_file, had_image, status):
    """Ghi lại 1 lượt sử dụng AI vào bảng usage_logs, phục vụ trang thống kê developer.
    Dùng kết nối riêng (open_write_db) vì hàm này thường được gọi từ BÊN TRONG generator
    streaming của /api/chat, lúc đó `g`/`get_db()` có thể đã bị teardown (xem giải thích
    ở open_write_db)."""
    try:
        conn = open_write_db()
        try:
            conn.execute(
                '''INSERT INTO usage_logs
                   (user_id, endpoint, subject, mode, message_chars, response_chars, had_file, had_image, status, created_at)
                   VALUES (?, 'chat', ?, ?, ?, ?, ?, ?, ?, ?)''',
                (user_id, subject, mode, message_chars, response_chars, int(bool(had_file)), int(bool(had_image)),
                 status, now_iso())
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        # Không để lỗi ghi log ảnh hưởng tới trải nghiệm chat của học sinh.
        pass


# ==========================================
# 0.3. CẤU HÌNH HỆ THỐNG (settings key-value) — do developer chỉnh qua /developer
# ==========================================
def get_setting(key, default=None):
    """Đọc 1 giá trị cấu hình từ bảng settings. Cố ý dùng kết nối RIÊNG (open_write_db(),
    không phải get_db()/g) vì hàm này còn được gọi từ BÊN TRONG generator streaming của
    /api/chat (qua stream_consolex_ai() -> đọc ai_model_override/ai_temperature_override),
    lúc đó `g._database` có thể đã bị teardown — xem giải thích chi tiết ở open_write_db()."""
    conn = open_write_db()
    try:
        row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
        return row['value'] if row and row['value'] is not None else default
    finally:
        conn.close()


def set_setting(key, value):
    conn = open_write_db()
    try:
        conn.execute(
            'INSERT INTO settings (key, value) VALUES (?, ?) '
            'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
            (key, value)
        )
        conn.commit()
    finally:
        conn.close()


def google_login_effective_enabled():
    """Developer có thể tạm tắt nút đăng nhập Google từ trang /developer mà không cần
    sửa .env. Nếu chưa từng đặt override, hành vi mặc định vẫn theo cấu hình .env."""
    override = get_setting('google_login_override', '')
    if override == 'off':
        return False
    if override == 'on':
        return bool(GOOGLE_OAUTH_ENABLED)  # không thể "bật" nếu chưa có Client ID/Secret
    return GOOGLE_OAUTH_ENABLED


# ==========================================
# 0.4. TUỲ CHỈNH CÁ NHÂN (preferences theo tài khoản)
# ==========================================
PREFERENCE_DEFAULTS = {
    'theme': 'system',            # 'light' | 'dark' | 'system'
    'language': 'vi',             # 'vi' | 'en'
    'default_subject': 'Toán',
    'default_mode': 'Giải thích',
    'default_thinking_mode': 'standard',  # 'standard' | 'scholar' | 'professor' | 'genius'
}
ALLOWED_PREFERENCE_KEYS = set(PREFERENCE_DEFAULTS.keys())


def get_user_preferences(user_id):
    db = get_db()
    row = db.execute('SELECT preferences FROM users WHERE id = ?', (user_id,)).fetchone()
    raw = row['preferences'] if row and row['preferences'] else '{}'
    try:
        prefs = json.loads(raw)
        if not isinstance(prefs, dict):
            prefs = {}
    except Exception:
        prefs = {}
    merged = dict(PREFERENCE_DEFAULTS)
    merged.update({k: v for k, v in prefs.items() if k in ALLOWED_PREFERENCE_KEYS})
    return merged


def save_user_preferences(user_id, updates):
    prefs = get_user_preferences(user_id)
    if isinstance(updates, dict):
        prefs.update({k: v for k, v in updates.items() if k in ALLOWED_PREFERENCE_KEYS and isinstance(v, str)})
    db = get_db()
    db.execute('UPDATE users SET preferences = ? WHERE id = ?', (json.dumps(prefs, ensure_ascii=False), user_id))
    db.commit()
    return prefs


# ==========================================
# 1. GIAO DIỆN ĐĂNG NHẬP / ĐĂNG KÝ
# ==========================================
# ==========================================
# 1.5 KẾT QUẢ THANH TOÁN VNPAY (trang trung gian sau khi quay lại từ VNPAY)
# ==========================================
VNPAY_RETURN_HTML = r'''
<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Kết quả thanh toán — StudyMate AI</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>tailwind.config = { darkMode: 'class' };</script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.6.0/css/all.min.css">
</head>
<body class="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-[#131313] p-4">
  <div class="max-w-md w-full bg-white dark:bg-[#1c1c1c] rounded-2xl shadow-lg border border-gray-100 dark:border-gray-800 p-8 text-center">
    {% if success %}
      <div class="w-16 h-16 rounded-full bg-emerald-100 dark:bg-emerald-900 text-emerald-500 flex items-center justify-center text-3xl mx-auto mb-4">
        <i class="fas fa-check"></i>
      </div>
      <h1 class="text-xl font-bold mb-2">Thanh toán thành công!</h1>
      <p class="text-sm text-gray-500 dark:text-gray-400">
        Đơn <strong>{{ order_code }}</strong> đã được ghi nhận.
        {% if status == 'paid' %}Gói <strong>{{ plan_label }}</strong> của em đã được kích hoạt — quay lại trang chat và tải lại trang để thấy thay đổi nhé! 🎉
        {% else %}Hệ thống đang xử lý, thường chỉ mất vài giây. Em quay lại trang chat và tải lại trang sau ít phút nhé.{% endif %}
      </p>
    {% else %}
      <div class="w-16 h-16 rounded-full bg-red-100 dark:bg-red-900 text-red-500 flex items-center justify-center text-3xl mx-auto mb-4">
        <i class="fas fa-xmark"></i>
      </div>
      <h1 class="text-xl font-bold mb-2">Thanh toán không thành công</h1>
      <p class="text-sm text-gray-500 dark:text-gray-400">
        {% if not valid %}Không xác thực được dữ liệu trả về từ VNPAY.{% else %}Giao dịch <strong>{{ order_code }}</strong> chưa hoàn tất hoặc đã bị huỷ.{% endif %}
        Em có thể thử lại ở hộp thoại "Nâng cấp gói".
      </p>
    {% endif %}
    <a href="{{ url_for('home') }}" class="inline-block mt-6 px-5 py-2.5 rounded-xl bg-blue-600 hover:bg-blue-700 text-white text-sm font-semibold">
      <i class="fas fa-arrow-left mr-1"></i> Về trang chat
    </a>
  </div>
</body>
</html>
'''

AUTH_HTML = r'''
<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{ 'Đăng nhập' if mode == 'login' else 'Đăng ký' }} — StudyMate AI</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>tailwind.config = { darkMode: 'class' };</script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.6.0/css/all.min.css">
  <style>
    body { font-family: 'Segoe UI', system-ui, sans-serif; }
    .auth-bg {
      background: radial-gradient(circle at 15% 20%, #4f46e5 0%, transparent 45%),
                  radial-gradient(circle at 85% 15%, #06b6d4 0%, transparent 40%),
                  radial-gradient(circle at 50% 90%, #6366f1 0%, transparent 45%),
                  linear-gradient(135deg, #0b1023 0%, #111827 100%);
    }
    .blob { position: absolute; border-radius: 9999px; filter: blur(70px); opacity: 0.35; animation: float 9s ease-in-out infinite; }
    @keyframes float { 0%,100% { transform: translateY(0) translateX(0); } 50% { transform: translateY(-25px) translateX(15px); } }
    .auth-card { animation: cardIn 0.5s cubic-bezier(.16,1,.3,1) both; }
    @keyframes cardIn { from { opacity: 0; transform: translateY(14px) scale(.98); } to { opacity: 1; transform: translateY(0) scale(1); } }
    .field-input:focus { box-shadow: 0 0 0 4px rgba(99,102,241,0.15); }
    .social-btn { transition: transform .15s ease, box-shadow .15s ease, background-color .15s ease; }
    .social-btn:hover { transform: translateY(-1px); box-shadow: 0 6px 16px rgba(0,0,0,0.08); }
    .social-btn:active { transform: translateY(0); }
    .primary-btn { transition: transform .15s ease, box-shadow .15s ease, opacity .15s ease; }
    .primary-btn:hover { transform: translateY(-1px); box-shadow: 0 10px 25px rgba(79,70,229,0.35); }
    .primary-btn:active { transform: translateY(0); }
  </style>
</head>
<body class="auth-bg min-h-screen flex items-center justify-center p-4 relative overflow-hidden">
  <div class="blob w-72 h-72 bg-indigo-500 top-[-4rem] left-[-4rem]"></div>
  <div class="blob w-80 h-80 bg-cyan-400 bottom-[-5rem] right-[-3rem]" style="animation-delay:-3s"></div>
  <div class="blob w-56 h-56 bg-violet-500 top-1/2 left-1/2" style="animation-delay:-6s"></div>

  <div class="w-full max-w-md relative z-10">
    <div class="text-center mb-7">
      <div class="inline-flex w-16 h-16 rounded-2xl bg-gradient-to-br from-indigo-500 to-cyan-400 items-center justify-center text-white text-3xl font-bold shadow-lg shadow-indigo-500/40">S</div>
      <h1 class="text-2xl font-extrabold mt-4 text-white tracking-tight">StudyMate AI</h1>
      <p class="text-indigo-200/80 text-sm mt-1">Gia sư AI thông minh cho học sinh THCS 🎓</p>
    </div>

    <div class="auth-card bg-white/95 backdrop-blur-xl rounded-3xl shadow-2xl border border-white/20 p-7 sm:p-8">
      <h2 class="text-xl font-bold text-gray-800 mb-1">{{ 'Chào mừng trở lại 👋' if mode == 'login' else 'Tạo tài khoản mới' }}</h2>
      <p class="text-sm text-gray-500 mb-6">{{ 'Đăng nhập để tiếp tục học cùng gia sư AI' if mode == 'login' else 'Chỉ mất chưa đến 1 phút để bắt đầu' }}</p>

      {% with messages = get_flashed_messages() %}
        {% if messages %}
          <div class="mb-5 space-y-2">
            {% for m in messages %}
              <div class="text-sm text-red-700 bg-red-50 border border-red-200 rounded-xl px-4 py-2.5 flex items-center gap-2">
                <i class="fas fa-circle-exclamation"></i> {{ m }}
              </div>
            {% endfor %}
          </div>
        {% endif %}
      {% endwith %}

      {% if google_enabled %}
      <div class="space-y-2.5 mb-5">
        <a href="{{ url_for('oauth_start', provider='google') }}"
           class="social-btn w-full flex items-center justify-center gap-3 px-4 py-3 rounded-xl border border-gray-200 bg-white hover:bg-gray-50 font-medium text-sm text-gray-700">
          <svg width="18" height="18" viewBox="0 0 48 48"><path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3c-1.6 4.6-6 8-11.3 8-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.9 1.2 8 3.1l5.7-5.7C34.6 6 29.6 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.7-.4-3.5z"/><path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.6 15.9 18.9 13 24 13c3.1 0 5.9 1.2 8 3.1l5.7-5.7C34.6 6 29.6 4 24 4 16.3 4 9.7 8.3 6.3 14.7z"/><path fill="#4CAF50" d="M24 44c5.5 0 10.4-1.9 14.3-5.1l-6.6-5.6C29.6 35.5 26.9 36.5 24 36.5c-5.3 0-9.7-3.4-11.3-8.1l-6.5 5C9.6 39.6 16.2 44 24 44z"/><path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.3-2.2 4.3-4.1 5.7l6.6 5.6C41.9 36.6 44 30.8 44 24c0-1.3-.1-2.7-.4-3.5z"/></svg>
          Đăng nhập với Google
        </a>
      </div>
      <div class="flex items-center gap-3 mb-5">
        <div class="flex-1 h-px bg-gray-200"></div>
        <span class="text-xs text-gray-400 font-medium">hoặc dùng tên đăng nhập</span>
        <div class="flex-1 h-px bg-gray-200"></div>
      </div>
      {% endif %}

      <form method="POST" class="space-y-4">
        <div>
          <label class="block text-sm font-medium text-gray-600 mb-1.5">Tên đăng nhập</label>
          <div class="relative">
            <i class="fas fa-user absolute left-4 top-1/2 -translate-y-1/2 text-gray-400 text-sm"></i>
            <input type="text" name="username" required maxlength="32" value="{{ username or '' }}"
                   class="field-input w-full pl-10 pr-4 py-3 bg-gray-50 border border-gray-200 rounded-xl focus:outline-none focus:border-indigo-400 text-gray-800 transition-shadow"
                   placeholder="vd: hocsinh2026" autocomplete="username">
          </div>
        </div>
        <div>
          <label class="block text-sm font-medium text-gray-600 mb-1.5">Mật khẩu</label>
          <div class="relative">
            <i class="fas fa-lock absolute left-4 top-1/2 -translate-y-1/2 text-gray-400 text-sm"></i>
            <input id="pwInput" type="password" name="password" required minlength="6"
                   class="field-input w-full pl-10 pr-11 py-3 bg-gray-50 border border-gray-200 rounded-xl focus:outline-none focus:border-indigo-400 text-gray-800 transition-shadow"
                   placeholder="Ít nhất 6 ký tự" autocomplete="{{ 'current-password' if mode == 'login' else 'new-password' }}">
            <button type="button" onclick="const i=document.getElementById('pwInput'); i.type = i.type==='password'?'text':'password'; this.querySelector('i').classList.toggle('fa-eye'); this.querySelector('i').classList.toggle('fa-eye-slash');"
                    class="absolute right-3.5 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600">
              <i class="fas fa-eye text-sm"></i>
            </button>
          </div>
        </div>
        {% if mode == 'register' %}
        <div>
          <label class="block text-sm font-medium text-gray-600 mb-1.5">Nhập lại mật khẩu</label>
          <div class="relative">
            <i class="fas fa-lock absolute left-4 top-1/2 -translate-y-1/2 text-gray-400 text-sm"></i>
            <input type="password" name="confirm" required minlength="6"
                   class="field-input w-full pl-10 pr-4 py-3 bg-gray-50 border border-gray-200 rounded-xl focus:outline-none focus:border-indigo-400 text-gray-800 transition-shadow"
                   placeholder="Nhập lại mật khẩu" autocomplete="new-password">
          </div>
        </div>
        {% endif %}
        <button type="submit"
                class="primary-btn w-full bg-gradient-to-r from-indigo-600 to-cyan-500 hover:opacity-95 text-white font-semibold py-3.5 rounded-xl shadow-lg shadow-indigo-500/30">
          {{ 'Đăng nhập' if mode == 'login' else 'Tạo tài khoản' }}
        </button>
      </form>

      <p class="text-center text-sm text-gray-500 mt-6">
        {% if mode == 'login' %}
          Chưa có tài khoản? <a href="{{ url_for('register_page') }}" class="text-indigo-600 font-semibold hover:underline">Đăng ký ngay</a>
        {% else %}
          Đã có tài khoản? <a href="{{ url_for('login_page') }}" class="text-indigo-600 font-semibold hover:underline">Đăng nhập</a>
        {% endif %}
      </p>
    </div>

    <p class="text-center text-xs text-indigo-200/60 mt-6">© {{ 2026 }} StudyMate AI — Dữ liệu đăng nhập được mã hoá, không chia sẻ cho bên thứ ba.</p>
  </div>
</body>
</html>
'''

# ==========================================
# 2. BIẾN HTML CHÍNH (GIAO DIỆN CHAT — kiểu ChatGPT/Claude)
# ==========================================
HTML = r'''
<!DOCTYPE html>
<html lang="vi" class="light">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{ app_name }}</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>tailwind.config = { darkMode: 'class' };</script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.6.0/css/all.min.css">
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
  <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
  <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js"></script>
  <style>
    html, body { height: 100%; }
    body { font-family: 'Segoe UI', system-ui, sans-serif; transition: background-color 0.2s, color 0.2s; }

    .ai-content { overflow-wrap: anywhere; word-break: break-word; }
    .ai-content p { margin-bottom: 0.6rem; }
    .ai-content p:last-child { margin-bottom: 0; }
    .ai-content .katex-display { overflow-x: auto; overflow-y: hidden; padding: 0.15rem 0; }
    .ai-content strong { color: #1e40af; }
    .dark .ai-content strong { color: #93c5fd; }
    .ai-content ul, .ai-content ol { padding-left: 1.4rem; margin-bottom: 0.6rem; }
    .ai-content li { list-style-type: disc; margin-bottom: 0.25rem; }
    .ai-content code { background: rgba(0,0,0,0.06); padding: 0.1rem 0.35rem; border-radius: 0.35rem; font-size: 0.9em; }
    .dark .ai-content code { background: rgba(255,255,255,0.1); }
    .ai-content pre { background: #1e293b; color: #e2e8f0; padding: 0.9rem; border-radius: 0.75rem; overflow-x: auto; margin-bottom: 0.6rem; }
    .ai-content pre code { background: transparent; padding: 0; }

    .typing-indicator span { display: inline-block; width: 6px; height: 6px; background-color: #9ca3af; border-radius: 50%; margin: 0 2px; animation: bounce 1.4s infinite ease-in-out both; }
    .typing-indicator span:nth-child(1) { animation-delay: -0.32s; }
    .typing-indicator span:nth-child(2) { animation-delay: -0.16s; }
    @keyframes bounce { 0%, 80%, 100% { transform: scale(0); } 40% { transform: scale(1); } }

    .stream-cursor { display: inline-block; width: 2px; height: 1em; background: currentColor; margin-left: 2px; vertical-align: text-bottom; animation: blink 0.9s steps(1) infinite; }
    @keyframes blink { 50% { opacity: 0; } }

    ::-webkit-scrollbar { width: 8px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 10px; }
    .dark ::-webkit-scrollbar-thumb { background: #4b5563; }

    #chatPanel { position: relative; }
    .drag-overlay {
      display: none;
      position: absolute; inset: 0; z-index: 40;
      background: rgba(37, 99, 235, 0.10);
      backdrop-filter: blur(2px);
      border: 3px dashed #3b82f6;
      border-radius: 1rem;
      align-items: center; justify-content: center;
      flex-direction: column; gap: 0.5rem;
      pointer-events: none;
      color: #1d4ed8;
      font-weight: 700; font-size: 1.2rem;
      margin: 0.75rem;
    }
    .dark .drag-overlay { color: #93c5fd; background: rgba(37, 99, 235, 0.18); }
    #chatPanel.drag-active .drag-overlay { display: flex; }

    .attachment-chip img { border: 1px solid rgba(0,0,0,0.08); }

    .msg-actions { opacity: 0; transition: opacity 0.15s; }
    /* addMessageActions() inserts the actions bar as a SIBLING right after the
       .ai-msg-group wrapper (wrapper.after(bar)), not as a child of it — so a
       descendant selector like ".ai-msg-group:hover .msg-actions" never matches
       and the "Báo lỗi" button stayed invisible (opacity: 0) forever, even though
       it was in the DOM and technically clickable. Use the general sibling
       combinator (~) instead, and also show it on its own hover/focus so touch
       devices (which have no :hover on the message) and keyboard users can reach it. */
    .msg-actions.force-visible,
    .ai-msg-group:hover ~ .msg-actions,
    .msg-actions:hover,
    .msg-actions:focus-within { opacity: 1; }

    /* ---------- Avatar "suy nghĩ" (shimmer chạy từ dưới lên trên) ----------
       Dải sáng quét dọc từ DƯỚI lên TRÊN, lặp lại, trên avatar robot — dùng làm
       avatar chung của cả website (sidebar, khung chat, chỉ báo đang gõ). Khi AI
       đang trả lời (.thinking) chạy nhanh & rõ hơn; ở logo sidebar (.brand-avatar)
       chạy chậm, mờ hơn như một nhịp "thở". */
    .ai-avatar { position: relative; overflow: hidden; isolation: isolate; }
    .ai-avatar::after {
      content: '';
      position: absolute; inset: -60% -20%;
      background: linear-gradient(0deg,
        transparent 0%, rgba(255,255,255,0) 38%, rgba(255,255,255,0.95) 50%,
        rgba(255,255,255,0) 62%, transparent 100%);
      background-size: 100% 260%;
      background-position: 0% 160%;
      mix-blend-mode: overlay;
      opacity: 0;
      pointer-events: none;
      will-change: background-position, opacity;
    }
    .ai-avatar.thinking::after { opacity: 1; animation: avatarShimmerUp 1.3s ease-in-out infinite; }
    .ai-avatar.brand-avatar::after { opacity: 0.55; animation: avatarShimmerUp 3.4s ease-in-out infinite; }
    @keyframes avatarShimmerUp {
      0%   { background-position: 0% 160%; }
      100% { background-position: 0% -160%; }
    }
    @media (prefers-reduced-motion: reduce) {
      .ai-avatar.thinking::after, .ai-avatar.brand-avatar::after { animation: none; opacity: 0.35; }
    }

    @keyframes memoryToastFade {
      0% { opacity: 0; transform: translate(-50%, 6px); }
      10%, 85% { opacity: 1; transform: translate(-50%, 0); }
      100% { opacity: 0; transform: translate(-50%, 6px); }
    }
    .memory-toast { left: 50%; animation: memoryToastFade 3.6s ease forwards; }

    #gamifyWidget { }
    .gamify-xp-track { background: #e5e7eb; border-radius: 999px; height: 6px; overflow: hidden; }
    .dark .gamify-xp-track { background: #374151; }
    .gamify-xp-fill { background: linear-gradient(90deg, #f59e0b, #f97316); height: 100%; border-radius: 999px; transition: width 0.4s; }

    #sidebar { transition: transform 0.2s ease; }
    @media (max-width: 1023px) { #sidebar { transform: translateX(-100%); } #sidebar.open { transform: translateX(0); } }

    .conv-item .conv-actions { opacity: 0; }
    .conv-item:hover .conv-actions { opacity: 1; }

    textarea#messageInput { max-height: 160px; }

    .modal-panel { animation: modalIn 0.15s ease; }
    @keyframes modalIn { from { opacity: 0; transform: translateY(8px) scale(0.98); } to { opacity: 1; transform: translateY(0) scale(1); } }
    .theme-opt { border-color: #e5e7eb; color: inherit; }
    .dark .theme-opt { border-color: #374151; }
    .theme-opt.active { border-color: #3b82f6; background: rgba(59,130,246,0.08); color: #2563eb; }
    .dark .theme-opt.active { color: #93c5fd; }

    .conv-menu { position: absolute; z-index: 45; min-width: 180px; }
  </style>
</head>
<body class="h-screen overflow-hidden bg-white dark:bg-[#212121] text-gray-800 dark:text-gray-100">

<div class="flex h-screen">

  <!-- ===================== SIDEBAR ===================== -->
  <aside id="sidebar" class="fixed lg:static inset-y-0 left-0 z-50 w-72 flex-shrink-0 bg-gray-50 dark:bg-[#171717] border-r border-gray-200 dark:border-gray-800 flex flex-col">
    <div class="p-3 flex items-center justify-between">
      <div class="flex items-center gap-2 px-1">
        <div class="ai-avatar brand-avatar w-8 h-8 rounded-lg bg-gradient-to-br from-blue-600 to-indigo-600 flex items-center justify-center text-white text-sm"><i class="fas fa-robot"></i></div>
        <span class="font-bold text-base truncate">{{ app_name }}</span>
      </div>
      <button id="closeSidebarBtn" class="lg:hidden w-8 h-8 flex items-center justify-center rounded-lg hover:bg-gray-200 dark:hover:bg-gray-800 text-gray-500">
        <i class="fas fa-xmark"></i>
      </button>
    </div>

    <div class="px-3 mt-1 space-y-2">
      <button id="newChatBtn" class="w-full flex items-center gap-2 px-3 py-2.5 rounded-xl border border-gray-300 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-800 font-medium text-sm transition-colors">
        <i class="fas fa-plus"></i> <span data-i18n="new_chat">Đoạn chat mới</span>
      </button>
      <div class="relative">
        <i class="fas fa-magnifying-glass absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 text-xs"></i>
        <input id="searchInput" type="text" placeholder="Tìm đoạn chat..." data-i18n-placeholder="search_placeholder"
          class="w-full pl-8 pr-3 py-2 text-sm bg-gray-100 dark:bg-gray-800 border-0 rounded-xl focus:outline-none focus:ring-2 focus:ring-blue-500 dark:text-white">
      </div>
    </div>

    <div class="flex-1 overflow-y-auto px-3 pb-3 mt-3 space-y-4 text-sm">
      <!-- Dự án (giống Claude Projects) -->
      <div>
        <div class="flex items-center justify-between mb-1 px-1">
          <span class="text-xs font-semibold text-gray-400 uppercase tracking-wide" data-i18n="projects">Dự án</span>
          <button id="newProjectBtn" class="w-5 h-5 flex items-center justify-center rounded hover:bg-gray-200 dark:hover:bg-gray-800 text-gray-400" title="Tạo dự án mới">
            <i class="fas fa-plus text-xs"></i>
          </button>
        </div>
        <div id="projectList" class="space-y-1"></div>
      </div>

      <!-- Đã ghim -->
      <div id="pinnedSection" class="hidden">
        <div class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-1 px-1" data-i18n="pinned">Đã ghim</div>
        <div id="pinnedList" class="space-y-1"></div>
      </div>

      <!-- Gần đây -->
      <div>
        <div class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-1 px-1" data-i18n="recents">Gần đây</div>
        <div id="convList" class="space-y-1"></div>
      </div>
    </div>

    <div id="gamifyWidget" class="hidden px-3 pb-2">
      <div class="rounded-xl bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-800 px-3 py-2.5">
        <div class="flex items-center justify-between text-xs mb-1.5">
          <span class="flex items-center gap-1 font-semibold text-orange-500"><i class="fas fa-fire"></i> <span id="gamifyStreak">0</span> ngày</span>
          <span class="text-gray-400">Cấp <span id="gamifyLevel" class="font-semibold text-gray-600 dark:text-gray-300">1</span></span>
        </div>
        <div class="gamify-xp-track"><div id="gamifyXpBar" class="gamify-xp-fill" style="width: 0%;"></div></div>
        <p id="gamifyXpText" class="text-[10px] text-gray-400 mt-1 text-right">0/100 XP</p>
      </div>
    </div>

    <div class="border-t border-gray-200 dark:border-gray-800 p-3 relative">
      <button id="userMenuBtn" type="button" class="w-full flex items-center gap-3 px-2 py-2 rounded-xl hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors">
        <div id="userAvatar" class="w-8 h-8 rounded-full bg-indigo-600 text-white flex items-center justify-center font-bold text-sm flex-shrink-0"></div>
        <span id="userNameLabel" class="flex-1 text-left truncate font-medium text-sm"></span>
        <i class="fas fa-chevron-up text-xs text-gray-400"></i>
      </button>
      <div id="userMenu" class="hidden absolute bottom-[64px] left-3 right-3 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl shadow-lg overflow-hidden z-10">
        <div class="px-4 py-2.5 border-b border-gray-100 dark:border-gray-700 flex items-center gap-1.5">
          <span class="text-[11px] font-semibold px-2 py-0.5 rounded-full {{ plan_meta[current_plan].badge }}">
            {{ plan_meta[current_plan].icon }} {{ plan_meta[current_plan].label }}
          </span>
          {% if is_plan_role_based %}
          <span class="text-[10px] text-gray-400">(theo vai trò {{ role_label }})</span>
          {% endif %}
        </div>
        {% if is_developer %}
        <a href="/developer" class="flex items-center gap-2 px-4 py-3 text-sm hover:bg-gray-100 dark:hover:bg-gray-700 text-indigo-600 dark:text-indigo-400 border-b border-gray-100 dark:border-gray-700">
          <i class="fas fa-chart-line"></i> Thống kê (Developer)
        </a>
        {% endif %}
        <button type="button" onclick="openModal('settingsModal')" class="w-full flex items-center gap-2 px-4 py-3 text-sm hover:bg-gray-100 dark:hover:bg-gray-700 text-left">
          <i class="fas fa-gear w-4 text-gray-400"></i> <span data-i18n="settings">Cài đặt</span>
        </button>
        <button type="button" onclick="openModal('helpModal')" class="w-full flex items-center gap-2 px-4 py-3 text-sm hover:bg-gray-100 dark:hover:bg-gray-700 text-left border-t border-gray-100 dark:border-gray-700">
          <i class="fas fa-circle-question w-4 text-gray-400"></i> <span data-i18n="help">Trợ giúp &amp; phím tắt</span>
        </button>
        <button type="button" onclick="openModal('upgradeModal')" class="w-full flex items-center gap-2 px-4 py-3 text-sm hover:bg-gray-100 dark:hover:bg-gray-700 text-left border-t border-gray-100 dark:border-gray-700">
          <i class="fas fa-bolt w-4 text-amber-500"></i> <span data-i18n="upgrade">Nâng cấp gói</span>
        </button>
        <a href="/logout" class="flex items-center gap-2 px-4 py-3 text-sm hover:bg-gray-100 dark:hover:bg-gray-700 text-red-600 dark:text-red-400 border-t border-gray-100 dark:border-gray-700">
          <i class="fas fa-right-from-bracket w-4"></i> <span data-i18n="logout">Đăng xuất</span>
        </a>
      </div>
    </div>
  </aside>
  <div id="sidebarOverlay" class="hidden fixed inset-0 bg-black/40 z-40 lg:hidden"></div>

  <!-- ===================== MAIN ===================== -->
  <div class="flex-1 flex flex-col min-w-0">

    <header class="flex items-center gap-2 px-3 lg:px-5 py-3 border-b border-gray-200 dark:border-gray-800 flex-wrap">
      <button id="openSidebarBtn" class="lg:hidden w-9 h-9 flex items-center justify-center rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-600 dark:text-gray-300">
        <i class="fas fa-bars"></i>
      </button>

      <select id="subject" class="text-sm font-medium bg-gray-100 dark:bg-gray-800 border-0 rounded-full pl-4 pr-8 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500 cursor-pointer dark:text-white">
        <option value="Toán">📐 Toán Học</option>
        <option value="Ngữ Văn">📖 Ngữ Văn</option>
        <option value="Tiếng Anh">🇬🇧 Tiếng Anh</option>
        <option value="Vật Lý">⚛️ Vật Lý</option>
        <option value="Hóa Học">🧪 Hóa Học</option>
        <option value="Sinh Học">🌱 Sinh Học</option>
        <option value="Lịch sử & Địa lý">🌍 Lịch sử & Địa lý</option>
        <option value="Tin Học">💻 Tin Học</option>
      </select>

      <select id="modeSelect" class="text-sm font-medium bg-gray-100 dark:bg-gray-800 border-0 rounded-full pl-4 pr-8 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500 cursor-pointer dark:text-white">
        <option value="Giải thích">📘 Giải Thích Dễ Hiểu</option>
        <option value="Gợi ý">💡 Gợi Ý Từng Bước</option>
        <option value="Kiểm tra bài làm">✅ Kiểm Tra Bài Làm</option>
        <option value="Luyện tập">📝 Ra Bài Luyện Tập</option>
        <option value="Ôn tập">🔄 Tổng Hợp Ôn Tập</option>
      </select>

      <div class="relative">
        <button id="thinkingModeBtn" type="button" title="Chế độ suy nghĩ của AI"
          class="text-sm font-medium bg-gray-100 dark:bg-gray-800 border-0 rounded-full pl-3.5 pr-2.5 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500 cursor-pointer dark:text-white flex items-center gap-1.5">
          <span id="thinkingModeIcon">💬</span>
          <span id="thinkingModeLabel">Trợ Lý</span>
          <i class="fas fa-chevron-down text-[10px] text-gray-400"></i>
        </button>
        <div id="thinkingModeMenu" class="hidden absolute left-0 top-full mt-1.5 z-30 w-72 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl shadow-lg overflow-hidden">
          {% for key in thinking_mode_order %}
          {% set tm = thinking_modes[key] %}
          {% set unlocked = key in unlocked_thinking_modes %}
          <button type="button" class="thinking-mode-item w-full text-left px-3.5 py-2.5 hover:bg-gray-50 dark:hover:bg-gray-700 flex items-start gap-2.5 text-sm border-b border-gray-50 dark:border-gray-700/50 last:border-b-0 {{ '' if unlocked else 'opacity-70' }}"
            data-mode="{{ key }}" data-unlocked="{{ '1' if unlocked else '0' }}">
            <span class="text-base leading-5">{{ tm.icon }}</span>
            <span class="flex-1 min-w-0">
              <span class="font-medium flex items-center gap-1.5 flex-wrap">
                {{ tm.label }}
                {% if not unlocked %}
                <span class="text-[10px] font-semibold px-1.5 py-0.5 rounded-full {{ plan_meta[tm.min_plan].badge }}">
                  <i class="fas fa-lock text-[9px] mr-0.5"></i>{{ plan_meta[tm.min_plan].label }}
                </span>
                {% endif %}
              </span>
              <span class="block text-xs text-gray-400 mt-0.5 leading-snug">{{ tm.desc }}</span>
            </span>
          </button>
          {% endfor %}
        </div>
      </div>

      <div class="flex-1"></div>

      <button onclick="startVoice()" class="w-9 h-9 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-600 dark:text-gray-300 flex items-center justify-center" title="Trợ lý giọng nói">
        <i class="fas fa-microphone"></i>
      </button>
      <button onclick="toggleTheme()" class="w-9 h-9 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-600 dark:text-gray-300 flex items-center justify-center" title="Đổi giao diện">
        <i id="themeIcon" class="fas fa-moon"></i>
      </button>
    </header>

    <div id="bannerBar" class="hidden items-center gap-2 px-4 py-2 bg-amber-50 dark:bg-amber-900/30 border-b border-amber-200 dark:border-amber-800 text-amber-800 dark:text-amber-200 text-sm">
      <i class="fas fa-bullhorn flex-shrink-0"></i>
      <span id="bannerText" class="flex-1"></span>
      <button onclick="dismissBanner()" class="w-6 h-6 flex items-center justify-center rounded hover:bg-amber-200/60 dark:hover:bg-amber-800/60 flex-shrink-0"><i class="fas fa-xmark text-xs"></i></button>
    </div>

    <div id="chatPanel" class="flex-1 overflow-y-auto scroll-smooth">
      <div class="drag-overlay">
        <i class="fas fa-cloud-arrow-up text-4xl"></i>
        <span>Thả file hoặc ảnh vào đây</span>
      </div>
      <div id="chat" class="max-w-3xl mx-auto px-4 py-6 space-y-6"></div>
    </div>

    <div class="border-t border-gray-200 dark:border-gray-800 p-3 lg:p-4">
      <div class="max-w-3xl mx-auto">
        <div id="attachmentsBar" class="hidden flex flex-wrap gap-2 mb-2"></div>
        <div class="flex items-end gap-2 bg-gray-100 dark:bg-gray-800 rounded-3xl px-2.5 py-2 border border-transparent focus-within:border-blue-400 dark:focus-within:border-blue-500 transition-colors">
          <button onclick="document.getElementById('fileInput').click()" class="w-10 h-10 rounded-full hover:bg-gray-200 dark:hover:bg-gray-700 flex items-center justify-center flex-shrink-0 text-gray-500 dark:text-gray-300" title="Đính kèm file hoặc ảnh">
            <i class="fas fa-paperclip"></i>
          </button>
          <input type="file" id="fileInput" class="hidden" accept="image/*,.pdf,.docx,.txt,.csv">

          <textarea id="messageInput" rows="1" class="flex-1 bg-transparent border-0 focus:outline-none focus:ring-0 resize-none py-2 text-[15px] dark:text-white" placeholder="Nhập câu hỏi... (Enter để gửi, Shift+Enter để xuống dòng)"></textarea>

          <button onclick="sendMessage()" id="sendBtn" class="w-10 h-10 rounded-full bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white flex items-center justify-center flex-shrink-0 transition-colors">
            <i class="fas fa-arrow-up"></i>
          </button>
        </div>
        <p class="text-center text-xs text-gray-400 dark:text-gray-500 mt-2">StudyMate AI có thể mắc lỗi — em nên kiểm tra lại các thông tin quan trọng nhé.</p>
      </div>
    </div>
  </div>
</div>

<!-- ===================== MODALS ===================== -->
<div id="modalBackdrop" class="hidden fixed inset-0 bg-black/50 z-[60] flex items-center justify-center p-4" onclick="if(event.target===this) closeAllModals()">

  <!-- Cài đặt -->
  <div id="settingsModal" class="hidden modal-panel bg-white dark:bg-gray-800 rounded-2xl shadow-2xl w-full max-w-md max-h-[85vh] overflow-y-auto">
    <div class="flex items-center justify-between px-5 py-4 border-b border-gray-100 dark:border-gray-700">
      <h3 class="font-bold text-lg flex items-center gap-2"><i class="fas fa-gear text-gray-400"></i> Cài đặt</h3>
      <button onclick="closeAllModals()" class="w-8 h-8 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center justify-center"><i class="fas fa-xmark"></i></button>
    </div>
    <div class="p-5 space-y-5">
      <div class="rounded-xl border border-gray-200 dark:border-gray-700 p-3.5 flex items-center gap-3">
        <div class="flex-1 min-w-0">
          <div class="flex items-center gap-1.5">
            <span id="planBadge" class="text-[11px] font-semibold px-2 py-0.5 rounded-full {{ plan_meta[current_plan].badge }}">{{ plan_meta[current_plan].icon }} {{ plan_meta[current_plan].label }}</span>
          </div>
          <p id="planQuotaText" class="text-xs text-gray-400 mt-1.5">Đang tải thông tin gói...</p>
          <div class="w-full h-1.5 bg-gray-100 dark:bg-gray-700 rounded-full mt-1.5 overflow-hidden">
            <div id="planQuotaBar" class="h-full bg-indigo-500 rounded-full" style="width: 0%;"></div>
          </div>
        </div>
        {% if current_plan != 'max' %}
        <button type="button" onclick="openModal('upgradeModal')" class="flex-shrink-0 text-xs font-semibold px-3 py-2 rounded-lg bg-amber-50 dark:bg-amber-900/30 text-amber-600 dark:text-amber-400 hover:bg-amber-100 dark:hover:bg-amber-900/50">
          <i class="fas fa-bolt mr-1"></i> Nâng cấp
        </button>
        {% endif %}
      </div>
      <div>
        <label class="text-sm font-semibold block mb-2">Giao diện</label>
        <div class="grid grid-cols-3 gap-2" id="themeOptions">
          <button type="button" data-theme="light" class="theme-opt px-3 py-2 rounded-xl border text-sm font-medium">☀️ Sáng</button>
          <button type="button" data-theme="dark" class="theme-opt px-3 py-2 rounded-xl border text-sm font-medium">🌙 Tối</button>
          <button type="button" data-theme="system" class="theme-opt px-3 py-2 rounded-xl border text-sm font-medium">💻 Hệ thống</button>
        </div>
      </div>
      <div>
        <label class="text-sm font-semibold block mb-2">Ngôn ngữ / Language</label>
        <select id="languageSelect" class="w-full px-3 py-2.5 rounded-xl bg-gray-100 dark:bg-gray-700 border-0 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:text-white">
          <option value="vi">Tiếng Việt</option>
          <option value="en">English</option>
        </select>
      </div>
      <div class="grid grid-cols-2 gap-3">
        <div>
          <label class="text-sm font-semibold block mb-2">Môn học mặc định</label>
          <select id="defaultSubjectSelect" class="w-full px-3 py-2.5 rounded-xl bg-gray-100 dark:bg-gray-700 border-0 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:text-white">
            <option value="Toán">Toán Học</option>
            <option value="Ngữ Văn">Ngữ Văn</option>
            <option value="Tiếng Anh">Tiếng Anh</option>
            <option value="Vật Lý">Vật Lý</option>
            <option value="Hóa Học">Hóa Học</option>
            <option value="Sinh Học">Sinh Học</option>
            <option value="Lịch sử & Địa lý">Lịch sử & Địa lý</option>
            <option value="Tin Học">Tin Học</option>
          </select>
        </div>
        <div>
          <label class="text-sm font-semibold block mb-2">Chế độ mặc định</label>
          <select id="defaultModeSelect" class="w-full px-3 py-2.5 rounded-xl bg-gray-100 dark:bg-gray-700 border-0 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:text-white">
            <option value="Giải thích">Giải Thích</option>
            <option value="Gợi ý">Gợi Ý</option>
            <option value="Kiểm tra bài làm">Kiểm Tra</option>
            <option value="Luyện tập">Luyện Tập</option>
            <option value="Ôn tập">Ôn Tập</option>
          </select>
        </div>
      </div>
      <div id="settingsSavedMsg" class="hidden text-sm text-emerald-600 dark:text-emerald-400 flex items-center gap-1"><i class="fas fa-check"></i> Đã lưu</div>
      <button onclick="savePreferences()" class="w-full bg-blue-600 hover:bg-blue-700 text-white font-semibold py-2.5 rounded-xl transition-colors">Lưu thay đổi</button>

      <div class="border-t border-gray-100 dark:border-gray-700 pt-4 space-y-2">
        <p class="text-xs font-semibold text-red-500 uppercase mb-2">Khu vực nguy hiểm</p>
        <button onclick="clearAllHistory()" class="w-full bg-red-50 dark:bg-red-900/20 hover:bg-red-100 dark:hover:bg-red-900/40 text-red-600 dark:text-red-400 font-medium py-2.5 rounded-xl text-sm transition-colors">
          <i class="fas fa-trash mr-1"></i> Xoá toàn bộ lịch sử trò chuyện
        </button>
        <button onclick="clearMyMemories()" class="w-full bg-purple-50 dark:bg-purple-900/20 hover:bg-purple-100 dark:hover:bg-purple-900/40 text-purple-600 dark:text-purple-400 font-medium py-2.5 rounded-xl text-sm transition-colors">
          <i class="fas fa-brain mr-1"></i> Xoá bộ nhớ AI của tôi
        </button>
      </div>
    </div>
  </div>

  <!-- Trợ giúp -->
  <div id="helpModal" class="hidden modal-panel bg-white dark:bg-gray-800 rounded-2xl shadow-2xl w-full max-w-md max-h-[85vh] overflow-y-auto">
    <div class="flex items-center justify-between px-5 py-4 border-b border-gray-100 dark:border-gray-700">
      <h3 class="font-bold text-lg flex items-center gap-2"><i class="fas fa-circle-question text-gray-400"></i> Trợ giúp &amp; phím tắt</h3>
      <button onclick="closeAllModals()" class="w-8 h-8 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center justify-center"><i class="fas fa-xmark"></i></button>
    </div>
    <div class="p-5 space-y-5 text-sm">
      <div>
        <p class="font-semibold mb-2">Phím tắt</p>
        <div class="space-y-1.5">
          <div class="flex justify-between"><span class="text-gray-500">Gửi câu hỏi</span><kbd class="px-2 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-xs font-mono">Enter</kbd></div>
          <div class="flex justify-between"><span class="text-gray-500">Xuống dòng</span><kbd class="px-2 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-xs font-mono">Shift + Enter</kbd></div>
          <div class="flex justify-between"><span class="text-gray-500">Đoạn chat mới</span><kbd class="px-2 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-xs font-mono">Ctrl/⌘ + K</kbd></div>
          <div class="flex justify-between"><span class="text-gray-500">Mở trợ giúp</span><kbd class="px-2 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-xs font-mono">Ctrl/⌘ + /</kbd></div>
        </div>
      </div>
      <div>
        <p class="font-semibold mb-2">Câu hỏi thường gặp</p>
        <div class="space-y-3 text-gray-600 dark:text-gray-300">
          <div><p class="font-medium text-gray-800 dark:text-gray-100">StudyMate đọc được file gì?</p><p>PDF, Word (.docx), .txt, .csv và ảnh (PNG/JPG/GIF/WEBP) — kéo-thả trực tiếp vào khung chat hoặc bấm nút 📎.</p></div>
          <div><p class="font-medium text-gray-800 dark:text-gray-100">Dữ liệu của em có bị mất không?</p><p>Lịch sử trò chuyện được lưu theo tài khoản, vẫn còn khi em đăng nhập lại trên thiết bị khác.</p></div>
          <div><p class="font-medium text-gray-800 dark:text-gray-100">"Dự án" dùng để làm gì?</p><p>Gom các đoạn chat cùng chủ đề (vd: "Ôn thi Học kỳ 2") lại một chỗ cho dễ tìm, giống thư mục.</p></div>
        </div>
      </div>
    </div>
  </div>

  <!-- Nâng cấp gói -->
  <div id="upgradeModal" class="hidden modal-panel bg-white dark:bg-gray-800 rounded-2xl shadow-2xl w-full max-w-3xl max-h-[85vh] overflow-y-auto">
    <div class="flex items-center justify-between px-5 py-4 border-b border-gray-100 dark:border-gray-700">
      <h3 class="font-bold text-lg flex items-center gap-2"><i class="fas fa-bolt text-amber-500"></i> Nâng cấp gói</h3>
      <button onclick="closeAllModals()" class="w-8 h-8 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center justify-center"><i class="fas fa-xmark"></i></button>
    </div>

    <!-- Bước 1: chọn gói -->
    <div id="upgradePlansView" class="p-5">
      {% if not payment_methods_enabled %}
      <p class="text-xs text-center text-gray-400 mb-4">🚧 Chưa cấu hình phương thức thanh toán nào — xem README để bật.</p>
      {% endif %}
      <div class="grid sm:grid-cols-3 gap-4">
        {% for p in plan_order %}
        {% set meta = plan_meta[p] %}
        {% set limits = plan_limits[p] %}
        {% set is_current = (p == current_plan) %}
        <div class="rounded-2xl p-4 relative flex flex-col {{ 'border-2 border-blue-500' if is_current else 'border border-gray-200 dark:border-gray-700' }}">
          {% if is_current %}
          <span class="absolute -top-2.5 left-4 bg-blue-500 text-white text-[10px] font-bold px-2 py-0.5 rounded-full">GÓI HIỆN TẠI</span>
          {% endif %}
          <p class="font-bold flex items-center gap-1.5">{{ meta.icon }} {{ meta.label }}</p>
          {% if p in plan_pricing %}
            {% if is_discount_eligible %}
            <span class="inline-flex self-start items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-full bg-rose-100 text-rose-600 dark:bg-rose-900/40 dark:text-rose-300 mt-1">
              <i class="fas fa-gift"></i> Giảm {{ discount_pct }}% — còn {{ discount_months_left }} tháng ưu đãi
            </span>
            <p class="mt-1 mb-2">
              <span class="text-xl font-extrabold">{{ discount_amounts[p]|vnd }}₫</span>
              <span class="text-sm font-medium text-gray-400 line-through ml-1">{{ plan_pricing[p]|vnd }}₫</span>
              <span class="text-xs font-normal text-gray-400">/ tháng</span>
            </p>
            {% else %}
            <p class="text-xl font-extrabold mt-1 mb-2">{{ plan_pricing[p]|vnd }}₫ <span class="text-xs font-normal text-gray-400">/ tháng</span></p>
            {% endif %}
          {% else %}
          <p class="text-xl font-extrabold mt-1 mb-2 text-gray-400">Miễn phí</p>
          {% endif %}
          <ul class="text-sm text-gray-500 dark:text-gray-400 space-y-1.5 my-1 flex-1">
            <li><i class="fas fa-check text-emerald-500 mr-1.5"></i>
              {% if limits.daily_uploads is none %}Đọc file &amp; ảnh không giới hạn{% else %}{{ limits.daily_uploads }} lượt đọc file/ảnh mỗi 24h{% endif %}
            </li>
            <li><i class="fas fa-check text-emerald-500 mr-1.5"></i> Mỗi file/ảnh tối đa {{ limits.max_file_mb }}MB</li>
            <li><i class="fas fa-check text-emerald-500 mr-1.5"></i> Chat &amp; lịch sử không giới hạn số đoạn</li>
            <li><i class="fas fa-check text-emerald-500 mr-1.5"></i> Dự án &amp; ghim đoạn chat</li>
            {% for key in unlocked_by_plan[p] %}
            <li><i class="fas fa-brain text-indigo-400 mr-1.5"></i> Chế độ suy nghĩ {{ thinking_modes[key].icon }} {{ thinking_modes[key].label }}</li>
            {% endfor %}
          </ul>
          {% if is_current %}
          <button disabled class="w-full mt-3 py-2 rounded-xl bg-gray-100 dark:bg-gray-700 text-gray-400 text-sm font-semibold">Gói hiện tại</button>
          {% elif p not in plan_pricing %}
          <button disabled class="w-full mt-3 py-2 rounded-xl bg-gray-100 dark:bg-gray-700 text-gray-400 text-sm font-semibold">—</button>
          {% elif not payment_methods_enabled %}
          <button disabled class="w-full mt-3 py-2 rounded-xl bg-blue-200 dark:bg-blue-900 text-blue-500 dark:text-blue-300 text-sm font-semibold cursor-not-allowed">Chưa khả dụng</button>
          {% else %}
          <button type="button" onclick="openCheckout('{{ p }}')" class="upgrade-buy-btn w-full mt-3 py-2 rounded-xl bg-blue-600 hover:bg-blue-700 text-white text-sm font-semibold">
            Nâng cấp — {{ (discount_amounts[p] if is_discount_eligible else plan_pricing[p])|vnd }}₫/tháng
          </button>
          {% endif %}
        </div>
        {% endfor %}
      </div>
      {% if is_plan_role_based %}
      <p class="text-xs text-center text-gray-400 mt-4"><i class="fas fa-circle-info mr-1"></i> Tài khoản {{ role_label }} được cấp gói Max vô điều kiện theo vai trò, không cần nâng cấp.</p>
      {% endif %}
    </div>

    <!-- Bước 2: chọn phương thức + thanh toán -->
    <div id="upgradeCheckoutView" class="hidden p-5">
      <button type="button" onclick="backToPlans()" class="text-sm text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 mb-4 flex items-center gap-1.5">
        <i class="fas fa-arrow-left"></i> Chọn gói khác
      </button>

      <div id="checkoutMethodPicker" class="space-y-3">
        <p class="text-sm font-semibold">Chọn phương thức thanh toán cho gói <span id="checkoutPlanLabel" class="text-blue-600 dark:text-blue-400"></span> — <span id="checkoutPlanAmount" class="font-bold"></span></p>
        {% if bank_transfer_enabled %}
        <button type="button" onclick="startCheckout('bank_transfer')" class="w-full flex items-center gap-3 border border-gray-200 dark:border-gray-700 hover:border-blue-500 rounded-xl px-4 py-3 text-left transition-colors">
          <i class="fas fa-qrcode text-xl text-emerald-500 w-6"></i>
          <span class="flex-1">
            <span class="block font-medium text-sm">Chuyển khoản ngân hàng (quét mã QR)</span>
            <span class="block text-xs text-gray-400">Ngân hàng bất kỳ, hoặc quét từ app MoMo / ZaloPay</span>
          </span>
          <i class="fas fa-chevron-right text-gray-300"></i>
        </button>
        {% endif %}
        {% if vnpay_enabled %}
        <button type="button" onclick="startCheckout('vnpay')" class="w-full flex items-center gap-3 border border-gray-200 dark:border-gray-700 hover:border-blue-500 rounded-xl px-4 py-3 text-left transition-colors">
          <i class="fas fa-credit-card text-xl text-indigo-500 w-6"></i>
          <span class="flex-1">
            <span class="block font-medium text-sm">Thẻ ATM nội địa / Visa / Mastercard / JCB</span>
            <span class="block text-xs text-gray-400">Thanh toán qua cổng VNPAY, bảo mật chuẩn ngân hàng</span>
          </span>
          <i class="fas fa-chevron-right text-gray-300"></i>
        </button>
        {% endif %}
      </div>

      <!-- Kết quả: mã QR chuyển khoản -->
      <div id="checkoutBankView" class="hidden text-center">
        <p class="text-sm text-gray-500 dark:text-gray-400 mb-3">Quét mã bằng app ngân hàng bất kỳ, MoMo hoặc ZaloPay:</p>
        <img id="checkoutQrImage" src="" alt="Mã QR chuyển khoản" class="mx-auto w-56 h-56 rounded-xl border border-gray-200 dark:border-gray-700 object-contain bg-white">
        <div class="mt-4 text-sm text-left max-w-xs mx-auto space-y-1.5 bg-gray-50 dark:bg-gray-900 rounded-xl p-4">
          <p class="flex justify-between"><span class="text-gray-400">Ngân hàng thụ hưởng</span><span class="font-medium" id="checkoutBankName"></span></p>
          <p class="flex justify-between"><span class="text-gray-400">Số tài khoản</span><span class="font-mono font-medium" id="checkoutBankAccNo"></span></p>
          <p class="flex justify-between"><span class="text-gray-400">Chủ tài khoản</span><span class="font-medium" id="checkoutBankAccName"></span></p>
          <p class="flex justify-between"><span class="text-gray-400">Số tiền</span><span class="font-bold" id="checkoutBankAmount"></span></p>
          <p class="flex justify-between items-center"><span class="text-gray-400">Nội dung CK (bắt buộc)</span>
            <span class="flex items-center gap-1.5"><span class="font-mono font-bold" id="checkoutBankContent"></span>
            <button type="button" onclick="copyCheckoutContent()" class="text-gray-400 hover:text-blue-500"><i class="fas fa-copy"></i></button></span>
          </p>
        </div>
        <p class="text-xs text-amber-600 dark:text-amber-400 mt-3"><i class="fas fa-triangle-exclamation mr-1"></i>Ghi ĐÚNG nội dung chuyển khoản ở trên để hệ thống đối chiếu đúng đơn của em.</p>
        <div id="checkoutWaitingStatus" class="mt-4 text-sm text-gray-500 dark:text-gray-400 flex items-center justify-center gap-2">
          <i class="fas fa-spinner fa-spin"></i> Đang chờ Admin xác nhận đã nhận được tiền...
        </div>
      </div>

      <!-- Kết quả: VNPAY -->
      <div id="checkoutVnpayView" class="hidden text-center py-6">
        <i class="fas fa-circle-notch fa-spin text-3xl text-blue-500 mb-3"></i>
        <p class="text-sm text-gray-500 dark:text-gray-400">Đang chuyển sang cổng thanh toán VNPAY...</p>
      </div>
    </div>
  </div>

  <div id="reportIssueModal" class="hidden modal-panel bg-white dark:bg-gray-800 rounded-2xl shadow-2xl w-full max-w-md max-h-[85vh] overflow-y-auto">
    <div class="flex items-center justify-between px-5 py-4 border-b border-gray-100 dark:border-gray-700">
      <h3 class="font-bold text-lg flex items-center gap-2"><i class="fas fa-flag text-red-500"></i> Báo lỗi câu trả lời</h3>
      <button onclick="closeAllModals()" class="w-8 h-8 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center justify-center"><i class="fas fa-xmark"></i></button>
    </div>
    <div class="p-5 space-y-3 text-sm">
      <p class="text-xs text-gray-400">Cho Thầy/Cô biết câu trả lời này có vấn đề gì (sai kiến thức, khó hiểu, lạc đề...) để đội ngũ StudyMate cải thiện AI nhé.</p>
      <textarea id="reportIssueText" rows="4" maxlength="1000" placeholder="Mô tả vấn đề em gặp phải..."
        class="w-full px-3 py-2.5 rounded-xl bg-gray-100 dark:bg-gray-800 border-0 focus:outline-none focus:ring-2 focus:ring-red-500 dark:text-white resize-none"></textarea>
      <button id="reportIssueSubmitBtn" onclick="submitReportIssue()" class="w-full px-4 py-2.5 rounded-xl bg-red-600 hover:bg-red-700 disabled:opacity-50 text-white text-sm font-semibold">Gửi báo cáo</button>
      <p id="reportIssueStatus" class="hidden text-xs text-emerald-600 dark:text-emerald-400 flex items-center gap-1"><i class="fas fa-check"></i> Đã gửi báo cáo, cảm ơn em! ✓</p>
    </div>
  </div>
</div>

<script>
const CURRENT_USERNAME = {{ username|tojson }};
const APP_NAME = {{ app_name|tojson }};
const PLAN_PRICING_JS = {{ plan_pricing|tojson }};
const PLAN_META_JS = {{ plan_meta|tojson }};
const DISCOUNT_AMOUNTS_JS = {{ discount_amounts|tojson }};
const IS_DISCOUNT_ELIGIBLE_JS = {{ is_discount_eligible|tojson }};
const ACHIEVEMENTS_META_JS = {{ achievements_meta|tojson }};
let uploadedFileContext = "";
let uploadedFileName = "";
let uploadedImageDataUrl = "";
let uploadedImageName = "";
let currentConversationId = null;
const html = document.documentElement;

marked.setOptions({ breaks: true });

// Dựng công thức toán ($$...$$, \(...\), \[...\]) thành hiển thị đẹp bằng KaTeX.
// throwOnError:false để không vỡ lỗi khi công thức đang gõ dở (lúc đang stream) —
// KaTeX sẽ tự render lại khi nội dung đầy đủ và hợp lệ.
function renderMathIn(el) {
  if (window.renderMathInElement) {
    try {
      renderMathInElement(el, {
        delimiters: [
          { left: '$$', right: '$$', display: true },
          { left: '\\[', right: '\\]', display: true },
          { left: '\\(', right: '\\)', display: false },
          { left: '$', right: '$', display: false }
        ],
        throwOnError: false
      });
    } catch (e) { /* bỏ qua, không làm hỏng luồng chat */ }
  }
}

document.getElementById('userNameLabel').textContent = CURRENT_USERNAME;
document.getElementById('userAvatar').textContent = (CURRENT_USERNAME || '?').trim().charAt(0).toUpperCase();

function toggleTheme() {
  // Nút bật/tắt nhanh trên thanh trên — chuyển thẳng sáng/tối và lưu lại vào Cài đặt
  // của tài khoản để lần đăng nhập sau vẫn giữ nguyên lựa chọn.
  const nowDark = !html.classList.contains('dark');
  applyTheme(nowDark ? 'dark' : 'light');
  fetch('/api/preferences', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ theme: nowDark ? 'dark' : 'light' })
  }).then(res => res.ok ? res.json() : null).then(prefs => { if (prefs) currentPreferences = prefs; }).catch(() => {});
}

// ---------- Sidebar (mobile) ----------
const sidebar = document.getElementById('sidebar');
const overlay = document.getElementById('sidebarOverlay');
function openSidebar() { sidebar.classList.add('open'); overlay.classList.remove('hidden'); }
function closeSidebar() { sidebar.classList.remove('open'); overlay.classList.add('hidden'); }
document.getElementById('openSidebarBtn').addEventListener('click', openSidebar);
document.getElementById('closeSidebarBtn').addEventListener('click', closeSidebar);
overlay.addEventListener('click', closeSidebar);

// ---------- User menu ----------
const userMenuBtn = document.getElementById('userMenuBtn');
const userMenu = document.getElementById('userMenu');
userMenuBtn.addEventListener('click', (e) => { e.stopPropagation(); userMenu.classList.toggle('hidden'); });
document.addEventListener('click', () => userMenu.classList.add('hidden'));

// ---------- Modals (Cài đặt / Trợ giúp / Nâng cấp) ----------
const modalBackdrop = document.getElementById('modalBackdrop');
function openModal(id) {
  userMenu.classList.add('hidden');
  document.querySelectorAll('.modal-panel').forEach(m => m.classList.add('hidden'));
  document.getElementById(id).classList.remove('hidden');
  modalBackdrop.classList.remove('hidden');
  modalBackdrop.classList.add('flex');
  if (id === 'upgradeModal') {
    stopCheckoutPolling();
    document.getElementById('upgradeCheckoutView').classList.add('hidden');
    document.getElementById('upgradePlansView').classList.remove('hidden');
  }
}
function closeAllModals() {
  modalBackdrop.classList.add('hidden');
  modalBackdrop.classList.remove('flex');
  document.querySelectorAll('.modal-panel').forEach(m => m.classList.add('hidden'));
  stopCheckoutPolling();
}

// ---------- Chế độ suy nghĩ (Trợ Lý / Học Giả / Giáo Sư / Thiên Tài) ----------
let currentThinkingMode = 'standard';
const thinkingModeBtn = document.getElementById('thinkingModeBtn');
const thinkingModeMenu = document.getElementById('thinkingModeMenu');
thinkingModeBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  thinkingModeMenu.classList.toggle('hidden');
});
document.addEventListener('click', () => thinkingModeMenu.classList.add('hidden'));
thinkingModeMenu.addEventListener('click', (e) => e.stopPropagation());

document.querySelectorAll('.thinking-mode-item').forEach(item => {
  item.addEventListener('click', () => {
    const mode = item.dataset.mode;
    const unlocked = item.dataset.unlocked === '1';
    thinkingModeMenu.classList.add('hidden');
    if (!unlocked) {
      openModal('upgradeModal');
      return;
    }
    currentThinkingMode = mode;
    const icon = item.querySelector('.text-base').textContent;
    const label = item.querySelector('.font-medium').childNodes[0].textContent.trim();
    document.getElementById('thinkingModeIcon').textContent = icon;
    document.getElementById('thinkingModeLabel').textContent = label;
  });
});

// ---------- Gói sử dụng (Free/Premium/Max) — hiển thị ở Cài đặt ----------
async function loadPlanInfo() {
  try {
    const res = await fetch('/api/plan');
    if (!res.ok) return;
    const data = await res.json();
    const badge = document.getElementById('planBadge');
    const text = document.getElementById('planQuotaText');
    const bar = document.getElementById('planQuotaBar');
    if (badge) badge.textContent = `${data.icon} ${data.label}`;
    if (data.daily_upload_limit === null) {
      if (text) text.textContent = data.is_role_based
        ? 'Đọc file & ảnh không giới hạn (theo vai trò tài khoản).'
        : 'Đọc file & ảnh không giới hạn.';
      if (bar) bar.style.width = '100%';
    } else {
      const pct = Math.min(100, Math.round((data.daily_uploads_used / data.daily_upload_limit) * 100));
      if (text) text.textContent = `Đã dùng ${data.daily_uploads_used}/${data.daily_upload_limit} lượt đọc file/ảnh trong 24h qua`;
      if (bar) bar.style.width = pct + '%';
    }
  } catch (e) { /* im lặng bỏ qua lỗi mạng */ }
}

// ---------- Nâng cấp gói (thanh toán: VNPAY / Chuyển khoản VietQR) ----------
function formatVnd(n) { return Number(n).toLocaleString('vi-VN') + '₫'; }

let checkoutPlan = null;
let checkoutPollTimer = null;

function openCheckout(plan) {
  checkoutPlan = plan;
  document.getElementById('upgradePlansView').classList.add('hidden');
  document.getElementById('upgradeCheckoutView').classList.remove('hidden');
  document.getElementById('checkoutMethodPicker').classList.remove('hidden');
  document.getElementById('checkoutBankView').classList.add('hidden');
  document.getElementById('checkoutVnpayView').classList.add('hidden');
  const meta = PLAN_META_JS[plan];
  document.getElementById('checkoutPlanLabel').textContent = `${meta.icon} ${meta.label}`;
  const amount = IS_DISCOUNT_ELIGIBLE_JS ? DISCOUNT_AMOUNTS_JS[plan] : PLAN_PRICING_JS[plan];
  document.getElementById('checkoutPlanAmount').innerHTML = IS_DISCOUNT_ELIGIBLE_JS
    ? `${formatVnd(amount)}/tháng <span class="text-gray-400 font-normal line-through">${formatVnd(PLAN_PRICING_JS[plan])}</span>`
    : `${formatVnd(amount)}/tháng`;
}

function backToPlans() {
  stopCheckoutPolling();
  document.getElementById('upgradeCheckoutView').classList.add('hidden');
  document.getElementById('upgradePlansView').classList.remove('hidden');
}

function stopCheckoutPolling() {
  if (checkoutPollTimer) { clearInterval(checkoutPollTimer); checkoutPollTimer = null; }
}

async function startCheckout(method) {
  document.getElementById('checkoutMethodPicker').classList.add('hidden');
  try {
    const res = await fetch('/api/checkout', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ plan: checkoutPlan, method })
    });
    const data = await res.json();
    if (!res.ok) {
      alert(data.error || 'Không tạo được đơn hàng.');
      document.getElementById('checkoutMethodPicker').classList.remove('hidden');
      return;
    }

    if (method === 'vnpay') {
      document.getElementById('checkoutVnpayView').classList.remove('hidden');
      window.location.href = data.redirectUrl;  // chuyển hẳn trang sang cổng VNPAY
      return;
    }

    // bank_transfer
    document.getElementById('checkoutBankView').classList.remove('hidden');
    document.getElementById('checkoutQrImage').src = data.qrImageUrl;
    document.getElementById('checkoutBankName').textContent = data.bankId;
    document.getElementById('checkoutBankAccNo').textContent = data.bankAccountNo;
    document.getElementById('checkoutBankAccName').textContent = data.bankAccountName;
    document.getElementById('checkoutBankAmount').textContent = formatVnd(data.amount);
    document.getElementById('checkoutBankContent').textContent = data.transferContent;

    stopCheckoutPolling();
    checkoutPollTimer = setInterval(() => pollCheckoutStatus(data.orderCode), 4000);
  } catch (e) {
    alert('Lỗi mạng khi tạo đơn hàng.');
    document.getElementById('checkoutMethodPicker').classList.remove('hidden');
  }
}

async function pollCheckoutStatus(orderCode) {
  try {
    const res = await fetch(`/api/checkout/${orderCode}/status`);
    if (!res.ok) return;
    const data = await res.json();
    if (data.status === 'paid') {
      stopCheckoutPolling();
      document.getElementById('checkoutWaitingStatus').innerHTML =
        '<i class="fas fa-circle-check text-emerald-500"></i> Đã xác nhận! Gói của em đã được nâng cấp 🎉';
      loadPlanInfo();
      setTimeout(() => { closeAllModals(); window.location.reload(); }, 1800);
    } else if (data.status === 'cancelled' || data.status === 'failed') {
      stopCheckoutPolling();
      document.getElementById('checkoutWaitingStatus').innerHTML =
        '<i class="fas fa-circle-xmark text-red-500"></i> Đơn hàng đã bị huỷ/thất bại. Em thử tạo đơn mới nhé.';
    }
  } catch (e) { /* im lặng bỏ qua lỗi mạng, thử lại ở lượt poll kế tiếp */ }
}

function copyCheckoutContent() {
  const text = document.getElementById('checkoutBankContent').textContent;
  navigator.clipboard.writeText(text).catch(() => {});
}

// ---------- Ngôn ngữ (i18n nhẹ cho các nhãn chính trong giao diện) ----------
const I18N = {
  vi: {
    new_chat: 'Đoạn chat mới', search_placeholder: 'Tìm đoạn chat...', projects: 'Dự án',
    pinned: 'Đã ghim', recents: 'Gần đây', settings: 'Cài đặt', help: 'Trợ giúp & phím tắt',
    upgrade: 'Nâng cấp gói', logout: 'Đăng xuất'
  },
  en: {
    new_chat: 'New chat', search_placeholder: 'Search chats...', projects: 'Projects',
    pinned: 'Pinned', recents: 'Recents', settings: 'Settings', help: 'Help & shortcuts',
    upgrade: 'Upgrade plan', logout: 'Log out'
  }
};
function applyLanguage(lang) {
  const dict = I18N[lang] || I18N.vi;
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.getAttribute('data-i18n');
    if (dict[key]) el.textContent = dict[key];
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
    const key = el.getAttribute('data-i18n-placeholder');
    if (dict[key]) el.placeholder = dict[key];
  });
}

// ---------- Giao diện (theme: sáng / tối / theo hệ thống) ----------
function applyTheme(theme) {
  const systemDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  const shouldDark = theme === 'dark' || (theme === 'system' && systemDark);
  html.classList.toggle('dark', shouldDark);
  const icon = document.getElementById('themeIcon');
  icon.classList.toggle('fa-moon', !shouldDark);
  icon.classList.toggle('fa-sun', shouldDark);
  document.querySelectorAll('.theme-opt').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.theme === theme);
  });
}
document.getElementById('themeOptions').addEventListener('click', (e) => {
  const btn = e.target.closest('.theme-opt');
  if (btn) applyTheme(btn.dataset.theme);
});

// ---------- Tuỳ chỉnh cá nhân (Cài đặt) — lưu theo tài khoản qua /api/preferences ----------
let currentPreferences = null;
async function loadPreferences() {
  try {
    const res = await fetch('/api/preferences');
    if (!res.ok) return;
    currentPreferences = await res.json();
    applyTheme(currentPreferences.theme || 'system');
    applyLanguage(currentPreferences.language || 'vi');
    document.getElementById('languageSelect').value = currentPreferences.language || 'vi';
    document.getElementById('defaultSubjectSelect').value = currentPreferences.default_subject || 'Toán';
    document.getElementById('defaultModeSelect').value = currentPreferences.default_mode || 'Giải thích';
    const subjectEl = document.getElementById('subject');
    const modeEl = document.getElementById('modeSelect');
    if (currentPreferences.default_subject) subjectEl.value = currentPreferences.default_subject;
    if (currentPreferences.default_mode) modeEl.value = currentPreferences.default_mode;
  } catch (e) { /* dùng mặc định nếu không tải được */ }
}

async function savePreferences() {
  const activeThemeBtn = document.querySelector('.theme-opt.active');
  const payload = {
    theme: activeThemeBtn ? activeThemeBtn.dataset.theme : 'system',
    language: document.getElementById('languageSelect').value,
    default_subject: document.getElementById('defaultSubjectSelect').value,
    default_mode: document.getElementById('defaultModeSelect').value,
  };
  try {
    const res = await fetch('/api/preferences', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
    });
    if (res.ok) {
      currentPreferences = await res.json();
      applyLanguage(currentPreferences.language);
      const msg = document.getElementById('settingsSavedMsg');
      msg.classList.remove('hidden');
      setTimeout(() => msg.classList.add('hidden'), 2000);
    }
  } catch (e) { alert('Không lưu được cài đặt, em thử lại nhé.'); }
}

async function clearAllHistory() {
  if (!confirm('Xoá TOÀN BỘ lịch sử trò chuyện? Hành động này không thể hoàn tác.')) return;
  try {
    await fetch('/api/conversations/all', { method: 'DELETE' });
    closeAllModals();
    newChat();
    loadConversations();
  } catch (e) { alert('Không xoá được lịch sử, em thử lại nhé.'); }
}

async function clearMyMemories() {
  if (!confirm('Xoá toàn bộ bộ nhớ AI về em? Hành động này không thể hoàn tác.')) return;
  try {
    await fetch('/api/memories', { method: 'DELETE' });
    alert('Đã xoá xong.');
  } catch (e) { alert('Không xoá được, em thử lại nhé.'); }
}

// ---------- Thông báo hệ thống (banner do developer đặt) ----------
let bannerDismissed = false;
async function loadBanner() {
  if (bannerDismissed) return;
  try {
    const res = await fetch('/api/banner');
    if (!res.ok) return;
    const data = await res.json();
    const bar = document.getElementById('bannerBar');
    if (data.message) {
      document.getElementById('bannerText').textContent = data.message;
      bar.classList.remove('hidden');
      bar.classList.add('flex');
    } else {
      bar.classList.add('hidden');
      bar.classList.remove('flex');
    }
  } catch (e) { /* bỏ qua */ }
}
function dismissBanner() {
  bannerDismissed = true;
  const bar = document.getElementById('bannerBar');
  bar.classList.add('hidden');
  bar.classList.remove('flex');
}

// ---------- Phím tắt bàn phím ----------
document.addEventListener('keydown', (e) => {
  const ctrlOrCmd = e.ctrlKey || e.metaKey;
  if (ctrlOrCmd && e.key.toLowerCase() === 'k') { e.preventDefault(); newChat(); }
  else if (ctrlOrCmd && e.key === '/') { e.preventDefault(); openModal('helpModal'); }
  else if (e.key === 'Escape') { closeAllModals(); }
});

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str == null ? '' : str;
  return div.innerHTML;
}

function scrollChatToBottom() {
  const panel = document.getElementById('chatPanel');
  panel.scrollTop = panel.scrollHeight;
}

// ---------- Render tin nhắn ----------
function showTypingIndicator() {
  const chat = document.getElementById('chat');
  const wrapper = document.createElement('div');
  wrapper.id = 'typingIndicator';
  wrapper.className = 'flex gap-3 items-start';
  wrapper.innerHTML = `<div class="ai-avatar thinking w-8 h-8 rounded-full bg-gradient-to-br from-blue-600 to-indigo-600 flex items-center justify-center text-white text-sm flex-shrink-0 mt-0.5"><i class="fas fa-robot"></i></div>
    <div class="ai-content flex-1 min-w-0 leading-relaxed pt-1.5"><span class="typing-indicator inline-flex items-center gap-1 text-gray-400"><span></span><span></span><span></span></span></div>`;
  chat.appendChild(wrapper);
  scrollChatToBottom();
}
function removeTypingIndicator() {
  const el = document.getElementById('typingIndicator');
  if (el) el.remove();
}

function addMessage(sender, content, isMarkdown = false, actionsCtx = null) {
  const chat = document.getElementById('chat');
  if (sender === 'user') {
    const div = document.createElement('div');
    div.className = 'ml-auto max-w-[80%] bg-gray-100 dark:bg-gray-700 rounded-2xl px-4 py-2.5 whitespace-pre-wrap break-words';
    div.textContent = content;
    chat.appendChild(div);
    scrollChatToBottom();
    return div;
  }
  const wrapper = document.createElement('div');
  wrapper.className = 'ai-msg-group flex gap-3 items-start';
  const avatar = document.createElement('div');
  avatar.className = 'ai-avatar w-8 h-8 rounded-full bg-gradient-to-br from-blue-600 to-indigo-600 flex items-center justify-center text-white text-sm flex-shrink-0 mt-0.5';
  avatar.innerHTML = '<i class="fas fa-robot"></i>';
  const bubble = document.createElement('div');
  bubble.className = 'ai-content flex-1 min-w-0 leading-relaxed pt-1.5';
  bubble.innerHTML = isMarkdown ? marked.parse(content) : escapeHtml(content).replace(/\n/g, '<br>');
  if (isMarkdown) renderMathIn(bubble);
  wrapper.appendChild(avatar);
  wrapper.appendChild(bubble);
  chat.appendChild(wrapper);
  if (actionsCtx) addMessageActions(wrapper, actionsCtx.conversationId, () => content);
  scrollChatToBottom();
  return bubble;
}

function createAiStreamBubble() {
  const chat = document.getElementById('chat');
  const wrapper = document.createElement('div');
  wrapper.className = 'ai-msg-group flex gap-3 items-start';
  wrapper.innerHTML = `<div class="ai-avatar thinking w-8 h-8 rounded-full bg-gradient-to-br from-blue-600 to-indigo-600 flex items-center justify-center text-white text-sm flex-shrink-0 mt-0.5"><i class="fas fa-robot"></i></div>
    <div class="ai-content flex-1 min-w-0 leading-relaxed pt-1.5"><span class="typing-indicator inline-flex items-center gap-1 text-gray-400"><span></span><span></span><span></span></span></div>`;
  chat.appendChild(wrapper);
  scrollChatToBottom();
  return wrapper.querySelector('.ai-content');
}
function updateAiStreamBubble(bubble, text, showCursor) {
  bubble.innerHTML = marked.parse(text) + (showCursor ? '<span class="stream-cursor"></span>' : '');
  renderMathIn(bubble);
  scrollChatToBottom();
}

// ---------- Báo lỗi câu trả lời ----------
function addMessageActions(wrapper, conversationId, getText) {
  const bar = document.createElement('div');
  bar.className = 'msg-actions flex items-center gap-1 mt-1 ml-11';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'text-xs text-gray-400 hover:text-red-500 px-2 py-1 -ml-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 flex items-center gap-1.5';
  btn.title = 'Báo lỗi câu trả lời này';
  btn.innerHTML = '<i class="fas fa-flag"></i> <span>Báo lỗi</span>';
  btn.addEventListener('click', () => openReportModal(conversationId, getText()));
  bar.appendChild(btn);
  wrapper.after(bar);
  return bar;
}

let reportContext = { conversationId: null, messageExcerpt: '' };
function openReportModal(conversationId, messageExcerpt) {
  reportContext = { conversationId: conversationId || null, messageExcerpt: (messageExcerpt || '').slice(0, 2000) };
  const textEl = document.getElementById('reportIssueText');
  const statusEl = document.getElementById('reportIssueStatus');
  if (textEl) textEl.value = '';
  if (statusEl) statusEl.classList.add('hidden');
  openModal('reportIssueModal');
  setTimeout(() => textEl && textEl.focus(), 50);
}

async function submitReportIssue() {
  const textEl = document.getElementById('reportIssueText');
  const statusEl = document.getElementById('reportIssueStatus');
  const btn = document.getElementById('reportIssueSubmitBtn');
  const description = textEl.value.trim();
  if (!description) { textEl.focus(); return; }
  btn.disabled = true;
  try {
    const res = await fetch('/api/report-issue', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        conversationId: reportContext.conversationId,
        messageExcerpt: reportContext.messageExcerpt,
        description
      })
    });
    if (res.ok) {
      statusEl.classList.remove('hidden');
      setTimeout(closeAllModals, 1200);
    } else {
      const data = await res.json().catch(() => ({}));
      alert(data.error || 'Không gửi được báo cáo.');
    }
  } catch (err) {
    alert('Lỗi mạng khi gửi báo cáo.');
  } finally {
    btn.disabled = false;
  }
}

// ---------- "Bộ nhớ" AI: toast khi ghi nhớ điều gì mới ----------
function showMemoryToast(text) {
  const toast = document.createElement('div');
  toast.className = 'memory-toast fixed bottom-24 left-1/2 z-50 bg-gray-900 dark:bg-gray-100 text-white dark:text-gray-900 text-xs px-4 py-2 rounded-full shadow-lg flex items-center gap-2 max-w-[90vw]';
  toast.innerHTML = `<i class="fas fa-brain text-purple-400"></i> <span class="truncate">Đã ghi nhớ: ${escapeHtml(text.slice(0, 80))}</span>`;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 3600);
}

// ---------- Gamification: XP / streak / thành tựu ----------
async function loadGamification() {
  try {
    const res = await fetch('/api/gamification');
    if (!res.ok) return;
    renderGamification(await res.json());
  } catch (e) { /* im lặng bỏ qua lỗi mạng */ }
}

function renderGamification(data) {
  const widget = document.getElementById('gamifyWidget');
  if (!widget) return;
  widget.classList.remove('hidden');
  document.getElementById('gamifyStreak').textContent = data.streak_days;
  document.getElementById('gamifyLevel').textContent = data.level;
  const pct = Math.round((data.xp_into_level / data.xp_per_level) * 100);
  document.getElementById('gamifyXpBar').style.width = pct + '%';
  document.getElementById('gamifyXpText').textContent = `${data.xp_into_level}/${data.xp_per_level} XP`;
}

function showGamifyToast(html) {
  const toast = document.createElement('div');
  toast.className = 'memory-toast fixed bottom-24 left-1/2 z-50 bg-amber-500 text-white text-xs px-4 py-2.5 rounded-full shadow-lg flex items-center gap-2 max-w-[90vw] font-semibold';
  toast.innerHTML = html;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 4200);
}

function handleGamifyEvent(g) {
  renderGamification({
    streak_days: g.streak_days, level: g.level,
    xp_into_level: g.xp % 100, xp_per_level: 100,
  });
  if (g.leveled_up) {
    showGamifyToast(`<i class="fas fa-arrow-up"></i> Lên cấp ${g.level}! 🎉`);
  }
  (g.new_achievements || []).forEach((code, i) => {
    const meta = ACHIEVEMENTS_META_JS[code];
    if (meta) setTimeout(() => showGamifyToast(`${meta.icon} Mở khoá thành tựu: ${meta.label}!`), 600 + i * 1500);
  });
}

function showWelcome() {
  addMessage('ai', `👋 Chào em! Thầy/Cô là **${APP_NAME}**.\n\nEm chọn **Môn học** và **Chế độ** ở phía trên, gõ câu hỏi rồi bấm Enter (hoặc nút gửi) nhé! Em cũng có thể đính kèm file (PDF/Word/txt/csv) hoặc ảnh bằng nút 📎, hay kéo-thả trực tiếp vào khung chat. 🚀`, true);
}

// ---------- Lịch sử hội thoại (theo tài khoản) + Dự án + Ghim + Tìm kiếm ----------
let allConversations = [];
let allProjects = [];
let activeProjectFilter = null; // null = tất cả, số = lọc theo dự án, 'none' = chưa gắn dự án nào
let openConvMenuId = null;

async function loadConversations() {
  try {
    const res = await fetch('/api/conversations');
    if (res.status === 401) { window.location.href = '/login'; return; }
    if (!res.ok) return;
    allConversations = await res.json();
    renderSidebarLists();
  } catch (e) { /* im lặng bỏ qua lỗi mạng khi tải danh sách */ }
}

async function loadProjects() {
  try {
    const res = await fetch('/api/projects');
    if (!res.ok) return;
    allProjects = await res.json();
    renderProjectList();
  } catch (e) { /* im lặng bỏ qua lỗi mạng */ }
}

function renderProjectList() {
  const container = document.getElementById('projectList');
  container.innerHTML = '';
  const allBtn = document.createElement('div');
  allBtn.className = 'flex items-center gap-2 rounded-xl px-3 py-2 cursor-pointer text-sm ' +
    (activeProjectFilter === null ? 'bg-gray-200 dark:bg-gray-800 font-medium' : 'hover:bg-gray-200 dark:hover:bg-gray-800 text-gray-500');
  allBtn.innerHTML = '<i class="fas fa-inbox w-4 text-center"></i><span>Tất cả đoạn chat</span>';
  allBtn.addEventListener('click', () => { activeProjectFilter = null; renderSidebarLists(); renderProjectList(); });
  container.appendChild(allBtn);

  allProjects.forEach(proj => {
    const row = document.createElement('div');
    row.className = 'conv-item group flex items-center gap-2 rounded-xl px-3 py-2 cursor-pointer text-sm ' +
      (activeProjectFilter === proj.id ? 'bg-gray-200 dark:bg-gray-800 font-medium' : 'hover:bg-gray-200 dark:hover:bg-gray-800');
    row.innerHTML = `<i class="fas fa-folder w-4 text-center text-amber-500"></i>
      <span class="flex-1 truncate">${escapeHtml(proj.name)}</span>
      <button class="conv-actions text-gray-400 hover:text-red-500 w-6 h-6 flex items-center justify-center flex-shrink-0" title="Xoá dự án">
        <i class="fas fa-trash-can text-xs"></i>
      </button>`;
    row.addEventListener('click', () => { activeProjectFilter = proj.id; renderSidebarLists(); renderProjectList(); });
    row.querySelector('button').addEventListener('click', async (e) => {
      e.stopPropagation();
      if (!confirm(`Xoá dự án "${proj.name}"? Các đoạn chat bên trong sẽ không bị xoá.`)) return;
      await fetch(`/api/projects/${proj.id}`, { method: 'DELETE' });
      if (activeProjectFilter === proj.id) activeProjectFilter = null;
      loadProjects();
      loadConversations();
    });
    container.appendChild(row);
  });
}

document.getElementById('newProjectBtn').addEventListener('click', async () => {
  const name = prompt('Tên dự án mới (vd: Ôn thi Học kỳ 2):');
  if (!name || !name.trim()) return;
  try {
    await fetch('/api/projects', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name.trim() })
    });
    loadProjects();
  } catch (e) { alert('Không tạo được dự án, em thử lại nhé.'); }
});

document.getElementById('searchInput').addEventListener('input', () => renderSidebarLists());

function renderSidebarLists() {
  const search = document.getElementById('searchInput').value.trim().toLowerCase();
  let filtered = allConversations;
  if (activeProjectFilter !== null) filtered = filtered.filter(c => c.project_id === activeProjectFilter);
  if (search) filtered = filtered.filter(c => (c.title || '').toLowerCase().includes(search));

  const pinned = filtered.filter(c => c.pinned);
  const recent = filtered.filter(c => !c.pinned);

  const pinnedSection = document.getElementById('pinnedSection');
  if (pinned.length) {
    pinnedSection.classList.remove('hidden');
    renderConvGroup('pinnedList', pinned);
  } else {
    pinnedSection.classList.add('hidden');
  }
  renderConvGroup('convList', recent, recent.length ? null : 'Chưa có đoạn chat nào');
}

function renderConvGroup(containerId, list, emptyText) {
  const container = document.getElementById(containerId);
  container.innerHTML = '';
  if (!list.length) {
    if (emptyText) container.innerHTML = `<div class="text-gray-400 text-xs px-2 py-4 text-center">${emptyText}</div>`;
    return;
  }
  list.forEach(conv => {
    const item = document.createElement('div');
    const active = conv.id === currentConversationId;
    item.className = 'conv-item group relative flex items-center gap-1 rounded-xl px-3 py-2 cursor-pointer transition-colors ' +
      (active ? 'bg-gray-200 dark:bg-gray-800' : 'hover:bg-gray-200 dark:hover:bg-gray-800');
    item.innerHTML = `
      ${conv.pinned ? '<i class="fas fa-thumbtack text-[10px] text-blue-500 flex-shrink-0"></i>' : ''}
      <span class="flex-1 truncate">${escapeHtml(conv.title || 'Đoạn chat mới')}</span>
      <button class="conv-actions text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 w-6 h-6 flex items-center justify-center flex-shrink-0 transition-opacity" title="Tuỳ chọn">
        <i class="fas fa-ellipsis text-xs"></i>
      </button>`;
    item.querySelector('span').addEventListener('click', () => openConversation(conv.id));
    item.querySelector('button').addEventListener('click', (e) => { e.stopPropagation(); toggleConvMenu(conv, item); });
    container.appendChild(item);
  });
}

function toggleConvMenu(conv, anchorEl) {
  document.querySelectorAll('.conv-menu').forEach(m => m.remove());
  if (openConvMenuId === conv.id) { openConvMenuId = null; return; }
  openConvMenuId = conv.id;

  const menu = document.createElement('div');
  menu.className = 'conv-menu bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl shadow-lg overflow-hidden text-sm';
  const projectOptions = allProjects.map(p =>
    `<button data-project-id="${p.id}" class="move-opt w-full text-left px-4 py-2 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center gap-2"><i class="fas fa-folder text-amber-500 w-4"></i>${escapeHtml(p.name)}</button>`
  ).join('');

  menu.innerHTML = `
    <button class="pin-opt w-full text-left px-4 py-2.5 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center gap-2">
      <i class="fas fa-thumbtack w-4 text-gray-400"></i> ${conv.pinned ? 'Bỏ ghim' : 'Ghim đoạn chat'}
    </button>
    <button class="rename-opt w-full text-left px-4 py-2.5 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center gap-2 border-t border-gray-100 dark:border-gray-700">
      <i class="fas fa-pen w-4 text-gray-400"></i> Đổi tên
    </button>
    <div class="border-t border-gray-100 dark:border-gray-700">
      <div class="px-4 pt-2 pb-1 text-[11px] font-semibold text-gray-400 uppercase">Chuyển vào dự án</div>
      ${projectOptions || '<div class="px-4 py-2 text-gray-400 text-xs">Chưa có dự án nào</div>'}
      ${conv.project_id ? '<button data-project-id="" class="move-opt w-full text-left px-4 py-2 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center gap-2 text-gray-500"><i class="fas fa-inbox w-4"></i>Bỏ khỏi dự án</button>' : ''}
    </div>
    <button class="delete-opt w-full text-left px-4 py-2.5 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center gap-2 text-red-600 dark:text-red-400 border-t border-gray-100 dark:border-gray-700">
      <i class="fas fa-trash-can w-4"></i> Xoá đoạn chat
    </button>`;

  document.body.appendChild(menu);
  const rect = anchorEl.getBoundingClientRect();
  menu.style.top = Math.min(rect.bottom + 4, window.innerHeight - 260) + 'px';
  menu.style.left = Math.min(rect.right - 200, window.innerWidth - 210) + 'px';

  menu.querySelector('.pin-opt').addEventListener('click', async (e) => {
    e.stopPropagation();
    await patchConversation(conv.id, { pinned: !conv.pinned });
    menu.remove(); openConvMenuId = null;
    loadConversations();
  });
  menu.querySelector('.rename-opt').addEventListener('click', async (e) => {
    e.stopPropagation();
    const title = prompt('Đổi tên đoạn chat:', conv.title || '');
    if (title && title.trim()) await patchConversation(conv.id, { title: title.trim() });
    menu.remove(); openConvMenuId = null;
    loadConversations();
  });
  menu.querySelectorAll('.move-opt').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const pid = btn.dataset.projectId ? parseInt(btn.dataset.projectId, 10) : null;
      await patchConversation(conv.id, { project_id: pid });
      menu.remove(); openConvMenuId = null;
      loadConversations();
    });
  });
  menu.querySelector('.delete-opt').addEventListener('click', (e) => {
    e.stopPropagation();
    menu.remove(); openConvMenuId = null;
    deleteConversation(conv.id);
  });
}
document.addEventListener('click', () => {
  document.querySelectorAll('.conv-menu').forEach(m => m.remove());
  openConvMenuId = null;
});

async function patchConversation(id, updates) {
  try {
    await fetch(`/api/conversations/${id}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(updates)
    });
  } catch (e) { alert('Không cập nhật được đoạn chat, em thử lại nhé.'); }
}

async function openConversation(id) {
  try {
    const res = await fetch(`/api/conversations/${id}/messages`);
    if (res.status === 401) { window.location.href = '/login'; return; }
    if (!res.ok) return;
    const messages = await res.json();
    currentConversationId = id;
    document.getElementById('chat').innerHTML = '';
    messages.forEach(m => addMessage(
      m.role === 'user' ? 'user' : 'ai',
      m.content,
      m.role !== 'user',
      m.role !== 'user' ? { conversationId: id } : null
    ));
    clearAttachments();
    closeSidebar();
    loadConversations();
  } catch (e) {
    addMessage('ai', '🔌 Không tải được đoạn chat này. Em thử lại nhé!', false);
  }
}

async function deleteConversation(id) {
  if (!confirm('Xóa đoạn chat này? Hành động này không thể hoàn tác.')) return;
  try {
    await fetch(`/api/conversations/${id}`, { method: 'DELETE' });
    if (id === currentConversationId) newChat();
    loadConversations();
  } catch (e) { /* im lặng bỏ qua lỗi mạng */ }
}

function newChat() {
  currentConversationId = null;
  document.getElementById('chat').innerHTML = '';
  clearAttachments();
  showWelcome();
  closeSidebar();
  loadConversations();
}
document.getElementById('newChatBtn').addEventListener('click', newChat);

// ---------- Gửi tin nhắn (streaming) ----------
async function sendMessage() {
  const input = document.getElementById('messageInput');
  const sendBtn = document.getElementById('sendBtn');
  const text = input.value.trim();
  if (!text) return;

  addMessage('user', text);
  input.value = "";
  input.style.height = 'auto';

  input.disabled = true;
  sendBtn.disabled = true;
  sendBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';

  const subjectEl = document.getElementById('subject');
  const modeEl = document.getElementById('modeSelect');
  const subjectText = subjectEl.options[subjectEl.selectedIndex].text.replace(/^\S+\s/, '');
  const modeValue = modeEl.value;

  const aiBubble = createAiStreamBubble();
  let fullText = '';
  let gotFirstToken = false;
  let handledError = false;
  const wasNewConversation = currentConversationId === null;

  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        subject: subjectText,
        mode: modeValue,
        message: text,
        fileContext: uploadedFileContext,
        fileName: uploadedFileName,
        imageData: uploadedImageDataUrl,
        conversationId: currentConversationId,
        thinkingMode: currentThinkingMode
      })
    });

    if (response.status === 401) { window.location.href = '/login'; return; }

    if (!response.ok || !response.body) {
      const data = await response.json().catch(() => ({}));
      updateAiStreamBubble(aiBubble, '⚠️ **Lỗi:** ' + (data.error || 'Không nhận được phản hồi từ server.'), false);
      handledError = true;
    } else {
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let sepIndex;
        while ((sepIndex = buffer.indexOf('\n\n')) !== -1) {
          const rawEvent = buffer.slice(0, sepIndex);
          buffer = buffer.slice(sepIndex + 2);
          if (!rawEvent.startsWith('data: ')) continue;

          let payload;
          try { payload = JSON.parse(rawEvent.slice(6)); } catch { continue; }

          if (payload.conversationId) {
            currentConversationId = payload.conversationId;
          } else if (payload.memory) {
            showMemoryToast(payload.memory);
          } else if (payload.gamify) {
            handleGamifyEvent(payload.gamify);
          } else if (payload.error) {
            updateAiStreamBubble(aiBubble, (fullText ? fullText + '\n\n' : '') + '⚠️ **Lỗi:** ' + payload.error, false);
            handledError = true;
          } else if (payload.token) {
            gotFirstToken = true;
            fullText += payload.token;
            updateAiStreamBubble(aiBubble, fullText, true);
          } else if (payload.done) {
            updateAiStreamBubble(aiBubble, fullText, false);
          }
        }
      }

      if (!gotFirstToken && !handledError) {
        updateAiStreamBubble(aiBubble, '⚠️ Thầy/Cô chưa nhận được phản hồi. Em thử lại nhé!', false);
      }

      if (gotFirstToken && !handledError) {
        addMessageActions(aiBubble.parentElement, currentConversationId, () => fullText);
      }
    }
  } catch (error) {
    updateAiStreamBubble(aiBubble, fullText || '🔌 Đã mất kết nối. Em kiểm tra lại mạng nhé!', false);
  } finally {
    // Tắt hiệu ứng "đang suy nghĩ" trên avatar khi đã có kết quả (xong hoặc lỗi).
    const avatarEl = aiBubble.parentElement && aiBubble.parentElement.querySelector('.ai-avatar');
    if (avatarEl) avatarEl.classList.remove('thinking');
    input.disabled = false;
    sendBtn.disabled = false;
    sendBtn.innerHTML = '<i class="fas fa-arrow-up"></i>';
    input.focus();
    loadConversations();
  }
}

document.getElementById('messageInput').addEventListener('keydown', function (e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

document.getElementById('messageInput').addEventListener('input', function () {
  this.style.height = 'auto';
  this.style.height = (this.scrollHeight < 160 ? this.scrollHeight : 160) + 'px';
});

function startVoice() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) return alert("Trình duyệt chưa hỗ trợ Giọng nói.");
  const recognition = new SpeechRecognition();
  recognition.lang = 'vi-VN';
  showTypingIndicator();
  recognition.onresult = (e) => {
    removeTypingIndicator();
    document.getElementById('messageInput').value = e.results[0][0].transcript;
    sendMessage();
  };
  recognition.onerror = () => removeTypingIndicator();
  recognition.onend = () => removeTypingIndicator();
  recognition.start();
}

// ---------- Đính kèm file / ảnh (chọn tay hoặc kéo-thả) ----------
function clearAttachments() {
  uploadedFileContext = ""; uploadedFileName = "";
  uploadedImageDataUrl = ""; uploadedImageName = "";
  const bar = document.getElementById('attachmentsBar');
  bar.innerHTML = '';
  bar.classList.add('hidden');
}

function showAttachmentChip(kind, name, thumbUrl) {
  const bar = document.getElementById('attachmentsBar');
  bar.classList.remove('hidden');
  const chipId = kind === 'image' ? 'chip-image' : 'chip-file';
  let chip = document.getElementById(chipId);
  if (!chip) {
    chip = document.createElement('div');
    chip.id = chipId;
    chip.className = 'attachment-chip flex items-center gap-2 bg-blue-50 dark:bg-gray-700 border border-blue-200 dark:border-gray-600 rounded-xl px-3 py-2 text-sm';
    bar.appendChild(chip);
  }
  const iconHtml = kind === 'image'
    ? `<img src="${thumbUrl}" class="w-7 h-7 rounded object-cover">`
    : '<i class="fas fa-file-lines text-blue-500"></i>';
  chip.innerHTML = `${iconHtml} <span class="truncate max-w-[140px]" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
    <button type="button" class="ml-1 text-gray-400 hover:text-red-500" title="Gỡ đính kèm"><i class="fas fa-xmark"></i></button>`;
  chip.querySelector('button').onclick = () => removeAttachment(kind);
}

function removeAttachment(kind) {
  if (kind === 'image') { uploadedImageDataUrl = ""; uploadedImageName = ""; }
  else { uploadedFileContext = ""; uploadedFileName = ""; }
  const chip = document.getElementById(kind === 'image' ? 'chip-image' : 'chip-file');
  if (chip) chip.remove();
  const bar = document.getElementById('attachmentsBar');
  if (!bar.children.length) bar.classList.add('hidden');
}

async function processFile(file) {
  if (!file) return;
  const lower = file.name.toLowerCase();
  const isImage = /\.(png|jpe?g|gif|webp)$/.test(lower);
  const isDoc = /\.(pdf|docx|txt|csv)$/.test(lower);

  if (!isImage && !isDoc) {
    addMessage('ai', `⚠️ Định dạng file **${file.name}** chưa được hỗ trợ. Em thử PDF, Word (.docx), .txt, .csv hoặc ảnh (PNG/JPG/GIF/WEBP) nhé!`, true);
    return;
  }

  const noticeDiv = addMessage('ai', `📎 Đang đọc file **${file.name}**...`, true);

  const formData = new FormData();
  formData.append('file', file);

  try {
    const response = await fetch('/api/upload', { method: 'POST', body: formData });
    if (response.status === 401) { window.location.href = '/login'; return; }
    const data = await response.json();

    if (data.error) {
      updateAiStreamBubble(noticeDiv, '⚠️ **Lỗi đọc file:** ' + data.error, false);
      return;
    }

    if (data.type === 'image') {
      uploadedImageDataUrl = data.dataUrl;
      uploadedImageName = file.name;
      showAttachmentChip('image', file.name, data.dataUrl);
      updateAiStreamBubble(noticeDiv, `✅ Thầy/Cô đã nhận ảnh **${file.name}**. Em có thể hỏi Thầy/Cô về nội dung trong ảnh nhé! 🖼️`, false);
    } else {
      uploadedFileContext = data.text || "";
      uploadedFileName = file.name;
      showAttachmentChip('file', file.name);
      const pageInfo = data.pages ? ` (${data.pages} trang)` : '';
      updateAiStreamBubble(noticeDiv, `✅ Thầy/Cô đã đọc xong file **${file.name}**${pageInfo}. Bây giờ em có thể hỏi bất cứ điều gì về nội dung file này nhé! 📖`, false);
    }
    loadPlanInfo();
  } catch (err) {
    updateAiStreamBubble(noticeDiv, '🔌 Không tải được file lên server. Em thử lại nhé!', false);
  }
}
document.getElementById('fileInput').addEventListener('change', function (e) {
  if (e.target.files.length) processFile(e.target.files[0]);
  e.target.value = '';
});

// Kéo - thả file/ảnh vào toàn bộ khung chat
const chatPanel = document.getElementById('chatPanel');
let dragCounter = 0;

['dragenter', 'dragover'].forEach(evtName => {
  chatPanel.addEventListener(evtName, (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files')) {
      dragCounter++;
      chatPanel.classList.add('drag-active');
    }
  });
});

['dragleave', 'dragend'].forEach(evtName => {
  chatPanel.addEventListener(evtName, (e) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounter = Math.max(0, dragCounter - 1);
    if (dragCounter === 0) chatPanel.classList.remove('drag-active');
  });
});

chatPanel.addEventListener('drop', (e) => {
  e.preventDefault();
  e.stopPropagation();
  dragCounter = 0;
  chatPanel.classList.remove('drag-active');
  const dt = e.dataTransfer;
  if (dt && dt.files && dt.files.length) {
    processFile(dt.files[0]);
  }
});

window.onload = () => {
  showWelcome();
  loadPreferences();
  loadProjects();
  loadConversations();
  loadBanner();
  loadPlanInfo();
  loadGamification();
};
</script>
</body>
</html>
'''

# ==========================================
# 3. BIẾN SECURITY_HTML (GIAO DIỆN BÁO CÁO BẢO MẬT)
# ==========================================
SECURITY_HTML = r'''
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bảo mật ứng dụng StudyMate AI với HTTPS</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script>tailwind.config = { darkMode: 'class' };</script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&display=swap');
        body { font-family: 'Inter', sans-serif; background-color: #f8fafc; color: #1e293b; }
        .chart-container { position: relative; width: 100%; max-width: 700px; margin: 0 auto; height: 350px; max-height: 400px; }
        .glass-card { background: rgba(255, 255, 255, 0.9); backdrop-filter: blur(10px); border: 1px solid rgba(226, 232, 240, 0.8); }
        .secure-border { border-left: 4px solid #10b981; }
        .step-circle { width: 32px; height: 32px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: bold; }
    </style>
</head>
<body class="antialiased">

    <nav class="sticky top-0 z-50 bg-white/80 backdrop-blur-md border-b border-gray-200">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="flex justify-between h-16 items-center">
                <div class="flex items-center gap-2">
                    <div class="p-2 bg-blue-600 rounded-lg text-white">
                        <span class="text-xl">🛡️</span>
                    </div>
                    <span class="font-bold text-xl tracking-tight">Security Architect</span>
                </div>
                <div class="hidden md:flex space-x-8 text-sm font-medium">
                    <a href="#analysis" class="hover:text-blue-600 transition">Phân tích App</a>
                    <a href="#proxy" class="hover:text-blue-600 transition">Reverse Proxy</a>
                    <a href="#mixed-content" class="hover:text-blue-600 transition">Mixed Content</a>
                    <a href="#verification" class="hover:text-blue-600 transition">Kiểm tra</a>
                </div>
                <a href="/" class="text-sm font-medium text-blue-600 hover:underline">← Về StudyMate</a>
            </div>
        </div>
    </nav>

    <main class="max-w-7xl mx-auto px-4 py-8 sm:px-6 lg:px-8">

        <header class="mb-12 text-center">
            <h1 class="text-4xl font-extrabold text-gray-900 mb-4">Nâng cấp Bảo mật cho StudyMate AI</h1>
            <p class="text-lg text-gray-600 max-w-3xl mx-auto">
                Làm thế nào để đưa ứng dụng <code class="bg-gray-200 px-2 py-1 rounded">app.py</code> từ môi trường Local lên môi trường Web Secured (HTTPS)
                với biểu tượng ổ khóa an toàn mà không cần thay đổi logic Flask.
            </p>
        </header>

        <section id="analysis" class="mb-16">
            <div class="bg-blue-50 p-6 rounded-2xl mb-8">
                <h2 class="text-2xl font-bold mb-3 flex items-center gap-2">
                    <span>🔍</span> 1. Phân tích Hiện trạng app.py
                </h2>
                <p class="text-gray-700">
                    Ứng dụng của bạn hiện đang chạy trên cổng <code class="font-mono text-blue-700">5000</code> qua giao thức HTTP.
                    Mặc dù code frontend sử dụng đường dẫn tương đối (<code class="font-mono">fetch('/api/chat')</code>), nhưng để đạt được trạng thái
                    "Connection is secure", chúng ta cần một lớp bao bọc bên ngoài để mã hóa dữ liệu.
                </p>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-2 gap-8 items-center">
                <div class="glass-card p-6 rounded-2xl shadow-sm border-t-4 border-blue-500">
                    <h3 class="font-bold text-lg mb-4">So sánh Chỉ số Bảo mật</h3>
                    <div class="chart-container">
                        <canvas id="securityRadar"></canvas>
                    </div>
                </div>
                <div class="space-y-4">
                    <div class="glass-card p-5 rounded-xl secure-border">
                        <h4 class="font-semibold text-emerald-700">✅ Điểm mạnh hiện tại</h4>
                        <ul class="mt-2 text-sm space-y-2">
                            <li>• Sử dụng <strong>Relative Paths</strong> trong JS giúp tránh lỗi Mixed Content cơ bản.</li>
                            <li>• API Endpoint tách biệt rõ ràng (/api/chat).</li>
                            <li>• Frontend Single-page dễ dàng triển khai qua Proxy.</li>
                        </ul>
                    </div>
                    <div class="glass-card p-5 rounded-xl border-left-4 border-red-500" style="border-left: 4px solid #ef4444;">
                        <h4 class="font-semibold text-red-700">❌ Điểm cần nâng cấp</h4>
                        <ul class="mt-2 text-sm space-y-2">
                            <li>• Dữ liệu gửi lên API chưa được mã hóa trên đường truyền.</li>
                            <li>• Thiếu chứng chỉ SSL/TLS hợp lệ (Nguyên nhân mất biểu tượng ổ khóa).</li>
                            <li>• Flask Server không nên tiếp xúc trực tiếp với Internet.</li>
                        </ul>
                    </div>
                </div>
            </div>
        </section>

        <section id="proxy" class="mb-16">
            <div class="bg-indigo-50 p-6 rounded-2xl mb-8">
                <h2 class="text-2xl font-bold mb-3 flex items-center gap-2">
                    <span>🌐</span> 2. Giải pháp Reverse Proxy (Nginx)
                </h2>
                <p class="text-gray-700">
                    Để giữ nguyên <code class="font-mono">app.py</code>, chúng ta sử dụng một "người đại diện" (Reverse Proxy).
                    Nginx sẽ đón nhận kết nối HTTPS (cổng 443), giải mã nó, rồi mới gửi yêu cầu tới Flask qua HTTP (cổng 5000) ở mạng nội bộ.
                </p>
            </div>

            <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
                <div class="lg:col-span-2 glass-card p-6 rounded-2xl">
                    <h3 class="font-bold mb-4">Mô hình Luồng dữ liệu Bảo mật</h3>
                    <div class="flex flex-col space-y-4">
                        <div class="flex items-center justify-between p-4 bg-white border rounded-xl">
                            <div class="flex items-center gap-3">
                                <span class="p-2 bg-gray-100 rounded">💻</span>
                                <div>
                                    <div class="font-bold text-sm text-emerald-600 uppercase tracking-wider">Trình duyệt (Client)</div>
                                    <div class="text-xs text-gray-500">Yêu cầu qua HTTPS (Port 443)</div>
                                </div>
                            </div>
                            <span class="text-emerald-500">🔒 Kết nối Bảo mật</span>
                        </div>
                        <div class="flex justify-center py-2">
                            <span class="text-2xl">⬇️</span>
                        </div>
                        <div class="flex items-center justify-between p-4 bg-blue-600 text-white rounded-xl shadow-lg">
                            <div class="flex items-center gap-3">
                                <span class="p-2 bg-white/20 rounded">🏢</span>
                                <div>
                                    <div class="font-bold text-sm uppercase tracking-wider">Nginx Reverse Proxy</div>
                                    <div class="text-xs text-blue-100">Xử lý chứng chỉ SSL/TLS</div>
                                </div>
                            </div>
                            <span class="text-xs bg-emerald-500 px-2 py-1 rounded">SSL Termination</span>
                        </div>
                        <div class="flex justify-center py-2">
                            <span class="text-2xl">⬇️</span>
                        </div>
                        <div class="flex items-center justify-between p-4 bg-gray-800 text-gray-300 rounded-xl">
                            <div class="flex items-center gap-3">
                                <span class="p-2 bg-white/10 rounded">🐍</span>
                                <div>
                                    <div class="font-bold text-sm uppercase tracking-wider">StudyMate Flask App</div>
                                    <div class="text-xs text-gray-400">Chạy tại Localhost:5000</div>
                                </div>
                            </div>
                            <span class="text-xs border border-gray-600 px-2 py-1 rounded">Không đổi Code</span>
                        </div>
                    </div>
                </div>
                <div class="bg-gray-900 rounded-2xl p-6 text-white overflow-hidden relative">
                    <div class="absolute top-0 right-0 p-4 opacity-10 text-6xl">⚙️</div>
                    <h3 class="font-bold mb-4 text-blue-400">Cấu hình Nginx gợi ý</h3>
                    <pre class="text-xs font-mono leading-relaxed text-gray-400 overflow-x-auto">
server {
    listen 443 ssl;
    server_name study-mate.ai;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
    }
}</pre>
                    <p class="mt-4 text-xs text-gray-500 italic">* proxy_buffering off giúp phản hồi dạng stream (SSE) tới trình duyệt ngay lập tức thay vì bị Nginx đệm lại.</p>
                </div>
            </div>
        </section>

        <section id="mixed-content" class="mb-16">
            <div class="bg-emerald-50 p-6 rounded-2xl mb-8">
                <h2 class="text-2xl font-bold mb-3 flex items-center gap-2">
                    <span>🛡️</span> 3. Ngăn ngừa lỗi "Mixed Content"
                </h2>
                <p class="text-gray-700">
                    Đây là lý do chính khiến biểu tượng ổ khóa biến mất hoặc có dấu chấm than. Trình duyệt chặn các yêu cầu không an toàn (HTTP) từ một trang an toàn (HTTPS).
                </p>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-2 gap-8">
                <div class="space-y-6">
                    <div class="flex gap-4">
                        <div class="step-circle bg-emerald-600 text-white flex-shrink-0">1</div>
                        <div>
                            <h4 class="font-bold">Sử dụng URL tương đối</h4>
                            <p class="text-sm text-gray-600">May mắn là trong code của bạn, <code>fetch('/api/chat')</code> đã là đường dẫn tương đối. Nó sẽ tự động dùng HTTPS nếu trang web đang chạy trên HTTPS.</p>
                        </div>
                    </div>
                    <div class="flex gap-4">
                        <div class="step-circle bg-emerald-600 text-white flex-shrink-0">2</div>
                        <div>
                            <h4 class="font-bold">Cập nhật Resource từ CDN</h4>
                            <p class="text-sm text-gray-600">Đảm bảo tất cả các script (Tailwind, FontAwesome, Marked) đều bắt đầu bằng <code>https://</code>. Code của bạn đã tuân thủ điều này.</p>
                        </div>
                    </div>
                    <div class="flex gap-4">
                        <div class="step-circle bg-emerald-600 text-white flex-shrink-0">3</div>
                        <div>
                            <h4 class="font-bold">Content Security Policy (CSP)</h4>
                            <p class="text-sm text-gray-600">Thêm thẻ meta để tự động nâng cấp các yêu cầu không an toàn: <br>
                                <code class="text-xs bg-gray-100 p-1 block mt-1">Content-Security-Policy: upgrade-insecure-requests</code>
                            </p>
                        </div>
                    </div>
                </div>
                <div class="glass-card p-6 rounded-2xl flex flex-col justify-center items-center text-center">
                    <div class="text-5xl mb-4">🔐</div>
                    <h3 class="text-xl font-bold text-emerald-600">Kết quả mong đợi</h3>
                    <p class="text-gray-500 mt-2 italic">Sau khi cấu hình HTTPS + Proxy đúng cách</p>
                    <div class="mt-4 p-4 border-2 border-emerald-500 bg-emerald-50 rounded-lg inline-flex items-center gap-2">
                        <span class="text-emerald-600 font-bold">🔒 Connection is secure</span>
                    </div>
                    <p class="mt-4 text-xs text-gray-400">Chứng chỉ SSL hợp lệ (Let's Encrypt / ZeroSSL) đã được tích hợp qua Proxy.</p>
                </div>
            </div>
        </section>

        <section id="verification" class="mb-16">
            <h2 class="text-2xl font-bold mb-8 text-center">Bảng điều khiển Trạng thái Bảo mật (Mô phỏng)</h2>
            <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
                <div class="glass-card p-4 rounded-xl text-center">
                    <div class="text-sm text-gray-500 mb-1">Giao thức</div>
                    <div class="text-xl font-bold text-emerald-600">HTTPS (TLS 1.3)</div>
                </div>
                <div class="glass-card p-4 rounded-xl text-center">
                    <div class="text-sm text-gray-500 mb-1">SSL Certificate</div>
                    <div class="text-xl font-bold text-emerald-600">Hợp lệ (90 ngày)</div>
                </div>
                <div class="glass-card p-4 rounded-xl text-center">
                    <div class="text-sm text-gray-500 mb-1">Mixed Content</div>
                    <div class="text-xl font-bold text-emerald-600">Không phát hiện</div>
                </div>
                <div class="glass-card p-4 rounded-xl text-center">
                    <div class="text-sm text-gray-500 mb-1">Điểm bảo mật</div>
                    <div class="text-xl font-bold text-emerald-600">A+</div>
                </div>
            </div>
        </section>

    </main>

    <footer class="bg-gray-900 text-gray-400 py-12 px-4">
        <div class="max-w-7xl mx-auto text-center">
            <p class="mb-4">Báo cáo được thực hiện cho dự án StudyMate AI</p>
            <div class="flex justify-center gap-6 text-sm">
                <span>Tình trạng mã nguồn: <span class="text-emerald-500">Giữ nguyên logic gốc</span></span>
                <span>Tiêu chuẩn: <span class="text-blue-500">Web Secured 2024</span></span>
            </div>
        </div>
    </footer>

    <script>
        const ctx = document.getElementById('securityRadar').getContext('2d');
        new Chart(ctx, {
            type: 'radar',
            data: {
                labels: ['Mã hóa dữ liệu', 'Danh tính (SSL)', 'Chống Sniffing', 'Tin cậy trình duyệt', 'Chống Mixed Content'],
                datasets: [{
                    label: 'HTTP (Hiện tại)',
                    data: [10, 5, 10, 20, 90],
                    fill: true,
                    backgroundColor: 'rgba(239, 68, 68, 0.2)',
                    borderColor: 'rgb(239, 68, 68)',
                    pointBackgroundColor: 'rgb(239, 68, 68)',
                }, {
                    label: 'HTTPS + Proxy (Đề xuất)',
                    data: [95, 100, 95, 95, 98],
                    fill: true,
                    backgroundColor: 'rgba(16, 185, 129, 0.2)',
                    borderColor: 'rgb(16, 185, 129)',
                    pointBackgroundColor: 'rgb(16, 185, 129)',
                }]
            },
            options: {
                maintainAspectRatio: false,
                scales: {
                    r: {
                        angleLines: { display: true },
                        suggestedMin: 0,
                        suggestedMax: 100
                    }
                },
                plugins: {
                    legend: { position: 'bottom' }
                }
            }
        });

        document.querySelectorAll('a[href^="#"]').forEach(anchor => {
            anchor.addEventListener('click', function (e) {
                e.preventDefault();
                document.querySelector(this.getAttribute('href')).scrollIntoView({
                    behavior: 'smooth'
                });
            });
        });
    </script>
</body>
</html>
'''

# ==========================================
# 3.1 BIẾN DEV_STATS_HTML (TRANG THỐNG KÊ — CHỈ DÀNH CHO DEVELOPER)
# ==========================================
DEV_STATS_HTML = r'''
<!DOCTYPE html>
<html lang="vi" class="light">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Thống kê sử dụng — StudyMate AI Max</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>tailwind.config = { darkMode: 'class' };</script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.6.0/css/all.min.css">
  <style>
    body { font-family: 'Segoe UI', system-ui, sans-serif; }
    .bar-track { display: flex; align-items: flex-end; gap: 6px; height: 140px; }
    .bar { flex: 1; background: linear-gradient(180deg, #6366f1, #4338ca); border-radius: 6px 6px 2px 2px; min-height: 3px; position: relative; }
    .dark .bar { background: linear-gradient(180deg, #818cf8, #4f46e5); }
    .bar:hover::after {
      content: attr(data-tip); position: absolute; bottom: 100%; left: 50%; transform: translateX(-50%);
      background: #111827; color: #fff; font-size: 11px; padding: 3px 7px; border-radius: 6px; white-space: nowrap; margin-bottom: 4px;
    }
    .progress-track { background: #e5e7eb; border-radius: 999px; height: 8px; overflow: hidden; }
    .dark .progress-track { background: #374151; }
    .progress-fill { background: linear-gradient(90deg, #4f46e5, #6366f1); height: 100%; border-radius: 999px; }
  </style>
</head>
<body class="min-h-screen bg-gray-50 dark:bg-[#131313] text-gray-800 dark:text-gray-100 transition-colors">

  <header class="sticky top-0 z-20 bg-white/80 dark:bg-[#171717]/80 backdrop-blur-xl border-b border-gray-200 dark:border-gray-800">
    <div class="max-w-6xl mx-auto px-4 sm:px-6 py-3.5 flex items-center gap-3">
      <div class="w-9 h-9 rounded-xl bg-gradient-to-br from-indigo-600 to-blue-600 flex items-center justify-center text-white font-bold">S</div>
      <div class="flex-1 min-w-0">
        <h1 class="font-bold text-base sm:text-lg leading-tight">Thống kê sử dụng</h1>
        <p class="text-xs text-gray-400">Khu vực Developer — chỉ tài khoản có quyền developer mới xem được</p>
      </div>
      <button onclick="document.documentElement.classList.toggle('dark')" class="w-9 h-9 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 flex items-center justify-center text-gray-500 dark:text-gray-300">
        <i class="fas fa-moon"></i>
      </button>
      <a href="{{ url_for('home') }}" class="text-sm font-semibold px-3.5 py-2 rounded-xl bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 whitespace-nowrap">
        <i class="fas fa-arrow-left mr-1"></i> Về trang chat
      </a>
    </div>
  </header>

  <main class="max-w-6xl mx-auto px-4 sm:px-6 py-6 space-y-6">

    {% with flashed = get_flashed_messages() %}
    {% if flashed %}
    <div class="space-y-2">
      {% for msg in flashed %}
      <div class="px-4 py-3 rounded-xl bg-indigo-50 dark:bg-indigo-900/30 border border-indigo-200 dark:border-indigo-800 text-indigo-700 dark:text-indigo-300 text-sm flex items-center gap-2">
        <i class="fas fa-circle-info"></i> {{ msg }}
      </div>
      {% endfor %}
    </div>
    {% endif %}
    {% endwith %}

    <!-- Thẻ tổng quan -->
    <div class="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-4">
      <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm">
        <div class="flex items-center justify-between">
          <span class="text-xs font-semibold text-gray-400 uppercase">Tổng tài khoản</span>
          <i class="fas fa-users text-indigo-500"></i>
        </div>
        <p class="text-3xl font-extrabold mt-2">{{ total_users }}</p>
        <p class="text-xs text-gray-400 mt-1">{{ new_users_7d }} tài khoản mới trong 7 ngày qua</p>
      </div>
      <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm">
        <div class="flex items-center justify-between">
          <span class="text-xs font-semibold text-gray-400 uppercase">Lượt hỏi AI (tổng)</span>
          <i class="fas fa-comments text-blue-500"></i>
        </div>
        <p class="text-3xl font-extrabold mt-2">{{ total_usage }}</p>
        <p class="text-xs text-gray-400 mt-1">{{ usage_today }} lượt hôm nay</p>
      </div>
      <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm">
        <div class="flex items-center justify-between">
          <span class="text-xs font-semibold text-gray-400 uppercase">7 ngày qua</span>
          <i class="fas fa-chart-line text-emerald-500"></i>
        </div>
        <p class="text-3xl font-extrabold mt-2">{{ usage_7d }}</p>
        <p class="text-xs text-gray-400 mt-1">Trung bình {{ avg_per_day_7d }} lượt / ngày</p>
      </div>
      <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm">
        <div class="flex items-center justify-between">
          <span class="text-xs font-semibold text-gray-400 uppercase">Tỉ lệ lỗi</span>
          <i class="fas fa-triangle-exclamation text-amber-500"></i>
        </div>
        <p class="text-3xl font-extrabold mt-2">{{ error_rate }}%</p>
        <p class="text-xs text-gray-400 mt-1">{{ error_count }} lượt gặp lỗi / {{ total_usage }} lượt</p>
      </div>
      <a href="#issue-reports" class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm hover:border-red-300 dark:hover:border-red-800 transition-colors">
        <div class="flex items-center justify-between">
          <span class="text-xs font-semibold text-gray-400 uppercase">Báo lỗi đang mở</span>
          <i class="fas fa-flag text-red-500"></i>
        </div>
        <p class="text-3xl font-extrabold mt-2">{{ open_issues_count }}</p>
        <p class="text-xs text-gray-400 mt-1">Bấm để xem chi tiết ↓</p>
      </a>
    </div>

    <!-- Biểu đồ 14 ngày gần nhất -->
    <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm">
      <h2 class="font-bold mb-4 flex items-center gap-2"><i class="fas fa-chart-column text-indigo-500"></i> Lượt sử dụng theo ngày (14 ngày gần nhất)</h2>
      {% if daily_counts %}
      <div class="bar-track">
        {% for d in daily_counts %}
        <div class="bar" style="height: {{ d.height }}%;" data-tip="{{ d.label }}: {{ d.count }} lượt"></div>
        {% endfor %}
      </div>
      <div class="flex justify-between text-[10px] text-gray-400 mt-2">
        <span>{{ daily_counts[0].label }}</span>
        <span>{{ daily_counts[-1].label }}</span>
      </div>
      {% else %}
      <p class="text-sm text-gray-400">Chưa có dữ liệu sử dụng.</p>
      {% endif %}
    </div>

    <div class="grid lg:grid-cols-2 gap-6">
      <!-- Theo môn học -->
      <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm">
        <h2 class="font-bold mb-4 flex items-center gap-2"><i class="fas fa-book text-blue-500"></i> Theo môn học</h2>
        <div class="space-y-3">
          {% for s in subject_stats %}
          <div>
            <div class="flex justify-between text-sm mb-1"><span>{{ s.subject }}</span><span class="text-gray-400">{{ s.count }} ({{ s.pct }}%)</span></div>
            <div class="progress-track"><div class="progress-fill" style="width: {{ s.pct }}%;"></div></div>
          </div>
          {% else %}
          <p class="text-sm text-gray-400">Chưa có dữ liệu.</p>
          {% endfor %}
        </div>
      </div>

      <!-- Theo chế độ học tập -->
      <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm">
        <h2 class="font-bold mb-4 flex items-center gap-2"><i class="fas fa-sliders text-emerald-500"></i> Theo chế độ học tập</h2>
        <div class="space-y-3">
          {% for m in mode_stats %}
          <div>
            <div class="flex justify-between text-sm mb-1"><span>{{ m.mode }}</span><span class="text-gray-400">{{ m.count }} ({{ m.pct }}%)</span></div>
            <div class="progress-track"><div class="progress-fill" style="width: {{ m.pct }}%;"></div></div>
          </div>
          {% else %}
          <p class="text-sm text-gray-400">Chưa có dữ liệu.</p>
          {% endfor %}
        </div>
      </div>
    </div>

    <!-- Người dùng hoạt động nhiều nhất -->
    <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm overflow-x-auto">
      <h2 class="font-bold mb-4 flex items-center gap-2"><i class="fas fa-ranking-star text-amber-500"></i> Người dùng hoạt động nhiều nhất</h2>
      <table class="w-full text-sm min-w-[420px]">
        <thead>
          <tr class="text-left text-gray-400 text-xs uppercase border-b border-gray-100 dark:border-gray-800">
            <th class="py-2 pr-3">Người dùng</th>
            <th class="py-2 pr-3">Vai trò</th>
            <th class="py-2 pr-3">Số lượt hỏi</th>
            <th class="py-2">Lần dùng gần nhất</th>
          </tr>
        </thead>
        <tbody>
          {% for u in top_users %}
          <tr class="border-b border-gray-50 dark:border-gray-900">
            <td class="py-2.5 pr-3 font-medium flex items-center gap-2">
              <div class="w-6 h-6 rounded-full bg-indigo-100 dark:bg-indigo-900 text-indigo-600 dark:text-indigo-300 flex items-center justify-center text-xs font-bold">{{ u.username[0]|upper }}</div>
              {{ u.username }}
            </td>
            <td class="py-2.5 pr-3">
              {% if u.role == 'developer' %}
                <span class="text-[11px] font-semibold px-2 py-0.5 rounded-full bg-indigo-100 dark:bg-indigo-900 text-indigo-600 dark:text-indigo-300">developer</span>
              {% else %}
                <span class="text-[11px] font-semibold px-2 py-0.5 rounded-full bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400">user</span>
              {% endif %}
            </td>
            <td class="py-2.5 pr-3">{{ u.usage_count }}</td>
            <td class="py-2.5 text-gray-400">{{ u.last_used or '—' }}</td>
          </tr>
          {% else %}
          <tr><td colspan="4" class="py-4 text-center text-gray-400">Chưa có lượt sử dụng nào.</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </div>

    <!-- Quản lý hệ thống -->
    <div class="grid lg:grid-cols-2 gap-6">
      <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm">
        <h2 class="font-bold mb-1 flex items-center gap-2"><i class="fas fa-bullhorn text-amber-500"></i> Thông báo hệ thống</h2>
        <p class="text-xs text-gray-400 mb-3">Hiển thị dạng banner cho tất cả người dùng ngay khi vào trang chat. Để trống rồi bấm Lưu để xoá thông báo.</p>
        <form method="POST" action="{{ url_for('developer_set_banner') }}" class="space-y-3">
          <textarea name="banner_message" rows="2" maxlength="300" placeholder="VD: Server sẽ bảo trì lúc 22h tối nay..."
            class="w-full px-3 py-2.5 rounded-xl bg-gray-100 dark:bg-gray-800 border-0 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 dark:text-white">{{ banner_message }}</textarea>
          <button type="submit" class="px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-semibold">Lưu thông báo</button>
        </form>
      </div>

      <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm">
        <h2 class="font-bold mb-1 flex items-center gap-2"><i class="fas fa-toggle-on text-emerald-500"></i> Đăng nhập bằng Google</h2>
        <p class="text-xs text-gray-400 mb-3">
          Cấu hình trong .env: <strong>{{ "đã thiết lập" if google_configured else "chưa thiết lập" }}</strong>.
          {% if not google_configured %}Cần đặt GOOGLE_CLIENT_ID/SECRET trước khi có thể bật.{% endif %}
        </p>
        <form method="POST" action="{{ url_for('developer_toggle_google_login') }}" class="flex flex-wrap gap-2">
          <button name="value" value="" type="submit" class="px-3 py-2 rounded-xl text-sm font-semibold {{ 'bg-gray-800 text-white' if google_override == '' else 'bg-gray-100 dark:bg-gray-800 text-gray-500' }}">Theo .env</button>
          <button name="value" value="on" type="submit" {{ 'disabled' if not google_configured else '' }} class="px-3 py-2 rounded-xl text-sm font-semibold disabled:opacity-40 {{ 'bg-emerald-600 text-white' if google_override == 'on' else 'bg-gray-100 dark:bg-gray-800 text-gray-500' }}">Bật</button>
          <button name="value" value="off" type="submit" class="px-3 py-2 rounded-xl text-sm font-semibold {{ 'bg-red-600 text-white' if google_override == 'off' else 'bg-gray-100 dark:bg-gray-800 text-gray-500' }}">Tắt</button>
        </form>
      </div>
    </div>

    <!-- Đơn nâng cấp gói (thanh toán) -->
    <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm">
      <div class="flex flex-wrap items-center justify-between gap-3 mb-1">
        <h2 class="font-bold flex items-center gap-2"><i class="fas fa-credit-card text-indigo-500"></i> Thanh toán nâng cấp gói</h2>
        <div class="flex items-center gap-2 text-xs">
          <span class="px-2 py-1 rounded-full {{ 'bg-emerald-100 text-emerald-600 dark:bg-emerald-900 dark:text-emerald-300' if vnpay_enabled else 'bg-gray-100 text-gray-400 dark:bg-gray-800' }}">
            <i class="fas fa-circle text-[6px] mr-1"></i>VNPAY {{ 'BẬT' if vnpay_enabled else 'TẮT' }}
          </span>
          <span class="px-2 py-1 rounded-full {{ 'bg-emerald-100 text-emerald-600 dark:bg-emerald-900 dark:text-emerald-300' if bank_transfer_enabled else 'bg-gray-100 text-gray-400 dark:bg-gray-800' }}">
            <i class="fas fa-circle text-[6px] mr-1"></i>VietQR {{ 'BẬT' if bank_transfer_enabled else 'TẮT' }}
          </span>
        </div>
      </div>
      <p class="text-xs text-gray-400 mb-4">
        Giá gói: Premium {{ plan_pricing.premium|vnd }}₫ · Max {{ plan_pricing.max|vnd }}₫ · Đã thu (đã xác nhận): <strong>{{ total_revenue|vnd }}₫</strong>.
        Đơn qua VNPAY tự động kích hoạt gói (không cần bấm gì); đơn Chuyển khoản VietQR cần Admin bấm xác nhận thủ công sau khi kiểm tra đã nhận được tiền.
      </p>

      {% if pending_orders %}
      <div class="mb-4">
        <div class="text-xs font-semibold text-amber-600 dark:text-amber-400 uppercase mb-2">
          <i class="fas fa-clock mr-1"></i> Đang chờ xác nhận ({{ pending_orders|length }})
        </div>
        <div class="space-y-2">
          {% for o in pending_orders %}
          <div class="flex flex-wrap items-center justify-between gap-2 border border-amber-200 dark:border-amber-900/50 bg-amber-50 dark:bg-amber-900/10 rounded-xl px-4 py-2.5 text-sm">
            <div>
              <span class="font-mono font-semibold">{{ o.order_code }}</span>
              — {{ o.username or '—' }} · {{ plan_meta[o.plan].icon }} {{ plan_meta[o.plan].label }} ·
              {{ o.amount|vnd }}₫ ·
              <span class="text-gray-400">{{ 'VietQR' if o.method == 'bank_transfer' else 'VNPAY' }}</span>
              <span class="text-gray-400">· {{ o.created_at[:16].replace('T', ' ') }}</span>
            </div>
            {% if o.method == 'bank_transfer' %}
            <div class="flex items-center gap-1.5">
              <form method="POST" action="{{ url_for('developer_confirm_payment', order_code=o.order_code) }}">
                <button type="submit" class="px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white text-xs font-semibold">
                  <i class="fas fa-check mr-1"></i>Xác nhận đã nhận tiền
                </button>
              </form>
              <form method="POST" action="{{ url_for('developer_cancel_payment', order_code=o.order_code) }}">
                <button type="submit" class="px-3 py-1.5 rounded-lg bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-xs font-semibold">Huỷ</button>
              </form>
            </div>
            {% else %}
            <span class="text-xs text-gray-400 italic">Chờ VNPAY xác nhận tự động...</span>
            {% endif %}
          </div>
          {% endfor %}
        </div>
      </div>
      {% endif %}

      <div class="text-xs font-semibold text-gray-400 uppercase mb-2">20 đơn gần nhất</div>
      <div class="overflow-x-auto">
        <table class="w-full text-sm min-w-[560px]">
          <thead>
            <tr class="text-left text-gray-400 text-xs uppercase border-b border-gray-100 dark:border-gray-800">
              <th class="py-2 pr-3">Mã đơn</th>
              <th class="py-2 pr-3">Người dùng</th>
              <th class="py-2 pr-3">Gói</th>
              <th class="py-2 pr-3">Số tiền</th>
              <th class="py-2 pr-3">Phương thức</th>
              <th class="py-2 pr-3">Trạng thái</th>
              <th class="py-2">Thời gian</th>
            </tr>
          </thead>
          <tbody>
            {% for o in recent_orders %}
            <tr class="border-b border-gray-50 dark:border-gray-900">
              <td class="py-2 pr-3 font-mono text-xs">{{ o.order_code }}</td>
              <td class="py-2 pr-3">{{ o.username or '—' }}</td>
              <td class="py-2 pr-3">{{ plan_meta[o.plan].icon }} {{ plan_meta[o.plan].label }}</td>
              <td class="py-2 pr-3">{{ o.amount|vnd }}₫</td>
              <td class="py-2 pr-3 text-gray-400">{{ 'VietQR' if o.method == 'bank_transfer' else 'VNPAY' }}</td>
              <td class="py-2 pr-3">
                {% if o.status == 'paid' %}<span class="text-emerald-600 dark:text-emerald-400 font-semibold">Đã thanh toán</span>
                {% elif o.status == 'pending' %}<span class="text-amber-600 dark:text-amber-400 font-semibold">Đang chờ</span>
                {% elif o.status == 'cancelled' %}<span class="text-gray-400">Đã huỷ</span>
                {% else %}<span class="text-red-500">Thất bại</span>{% endif %}
              </td>
              <td class="py-2 text-gray-400">{{ o.created_at[:16].replace('T', ' ') }}</td>
            </tr>
            {% else %}
            <tr><td colspan="7" class="py-4 text-center text-gray-400">Chưa có đơn nào.</td></tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>

    <!-- Báo cáo lỗi từ học sinh -->
    <div id="issue-reports" class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm scroll-mt-20">
      <div class="flex items-center justify-between mb-1 flex-wrap gap-2">
        <h2 class="font-bold flex items-center gap-2"><i class="fas fa-flag text-red-500"></i> Báo cáo lỗi từ học sinh</h2>
        <span class="text-xs text-gray-400">{{ open_issues_count }} đang mở / {{ issue_reports|length }} hiển thị</span>
      </div>
      <p class="text-xs text-gray-400 mb-4">Học sinh bấm "Báo lỗi" dưới 1 câu trả lời trong khung chat để gửi báo cáo về đây.</p>
      <div class="space-y-3">
        {% for r in issue_reports %}
        <div class="border border-gray-100 dark:border-gray-800 rounded-xl p-4 {{ 'opacity-50' if r.status == 'resolved' else '' }}" data-issue-id="{{ r.id }}">
          <div class="flex items-start justify-between gap-3 flex-wrap">
            <div class="min-w-0">
              <div class="flex items-center gap-2 text-xs text-gray-400 mb-1 flex-wrap">
                <span class="font-semibold text-gray-600 dark:text-gray-300">{{ r.username or '—' }}</span>
                <span>•</span><span>{{ r.created_at[:16].replace('T', ' ') }}</span>
                {% if r.status == 'resolved' %}
                <span class="px-1.5 py-0.5 rounded-full bg-emerald-100 dark:bg-emerald-900 text-emerald-600 dark:text-emerald-300 text-[10px] font-semibold uppercase">Đã xử lý</span>
                {% else %}
                <span class="px-1.5 py-0.5 rounded-full bg-red-100 dark:bg-red-900 text-red-600 dark:text-red-300 text-[10px] font-semibold uppercase">Đang mở</span>
                {% endif %}
              </div>
              <p class="text-sm font-medium">{{ r.description }}</p>
              {% if r.message_excerpt %}
              <p class="text-xs text-gray-400 mt-1.5 italic">Liên quan tới câu trả lời: "{{ r.message_excerpt[:160] }}{{ '…' if r.message_excerpt|length > 160 else '' }}"</p>
              {% endif %}
            </div>
            <form method="POST" action="{{ url_for('developer_resolve_issue', issue_id=r.id) }}">
              <button type="submit" class="flex-shrink-0 text-xs font-medium px-3 py-1.5 rounded-lg bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 whitespace-nowrap">
                {{ 'Mở lại' if r.status == 'resolved' else 'Đánh dấu đã xử lý' }}
              </button>
            </form>
          </div>
        </div>
        {% else %}
        <p class="text-sm text-gray-400">Chưa có báo cáo lỗi nào. 🎉</p>
        {% endfor %}
      </div>
    </div>

    <!-- Toàn bộ tài khoản -->
    <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm overflow-x-auto">
      <div class="flex flex-wrap items-center justify-between gap-3 mb-4">
        <h2 class="font-bold flex items-center gap-2"><i class="fas fa-address-card text-gray-500"></i> Toàn bộ tài khoản ({{ total_users }})</h2>
        <div class="flex items-center gap-2">
          <input id="userSearchInput" type="text" placeholder="Tìm theo tên đăng nhập..." oninput="filterUserTable()"
            class="px-3 py-1.5 rounded-lg bg-gray-100 dark:bg-gray-800 border-0 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 dark:text-white">
          <a href="{{ url_for('developer_export_csv') }}" class="text-xs font-semibold px-3 py-1.5 rounded-lg bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 whitespace-nowrap">
            <i class="fas fa-download mr-1"></i> Xuất CSV
          </a>
          {% if is_super_admin %}
          <a href="{{ url_for('developer_audit_log') }}" class="text-xs font-semibold px-3 py-1.5 rounded-lg bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 whitespace-nowrap">
            <i class="fas fa-scroll mr-1"></i> Nhật ký hệ thống
          </a>
          {% endif %}
        </div>
      </div>
      <table class="w-full text-sm min-w-[720px]">
        <thead>
          <tr class="text-left text-gray-400 text-xs uppercase border-b border-gray-100 dark:border-gray-800">
            <th class="py-2 pr-3">ID</th>
            <th class="py-2 pr-3">Người dùng</th>
            <th class="py-2 pr-3">Vai trò</th>
            <th class="py-2 pr-3">Gói</th>
            <th class="py-2 pr-3">Ngày tạo</th>
            <th class="py-2 text-right">Hành động</th>
          </tr>
        </thead>
        <tbody id="usersTableBody">
          {% for u in all_users %}
          <tr class="border-b border-gray-50 dark:border-gray-900" data-username="{{ u.username|lower }}">
            <td class="py-2.5 pr-3 text-gray-400">#{{ u.id }}</td>
            <td class="py-2.5 pr-3 font-medium">{{ u.username }}</td>
            <td class="py-2.5 pr-3">
              <span class="text-[11px] font-semibold px-2 py-0.5 rounded-full {{ role_meta[u.role].badge }}">
                {{ role_meta[u.role].icon }} {{ role_meta[u.role].label }}
              </span>
              {% if u.is_locked %}<span class="ml-1 text-[10px] font-semibold px-1.5 py-0.5 rounded bg-red-600 text-white">ĐÃ KHOÁ</span>{% endif %}
            </td>
            <td class="py-2.5 pr-3">
              <span class="text-[11px] font-semibold px-2 py-0.5 rounded-full {{ plan_meta[u.effective_plan].badge }}">
                {{ plan_meta[u.effective_plan].icon }} {{ plan_meta[u.effective_plan].label }}
              </span>
              {% if u.plan_is_role_based %}<span class="ml-1 text-[10px] text-gray-400 italic">(theo vai trò)</span>{% endif %}
            </td>
            <td class="py-2.5 pr-3 text-gray-400">{{ u.created_at[:10] }}</td>
            <td class="py-2.5 text-right">
              {% if u.id == current_user_id_val %}
                <span class="text-xs text-gray-400 italic">(bạn)</span>
              {% elif u.role == 'super_admin' %}
                <span class="text-xs text-gray-400 italic">—</span>
              {% elif u.role == 'admin' and not is_super_admin %}
                <span class="text-xs text-gray-400 italic">Chỉ Super Admin quản lý được</span>
              {% else %}
              <div class="flex items-center justify-end gap-1.5 flex-wrap">
                <form method="POST" action="{{ url_for('developer_change_role', user_id=u.id) }}" class="inline-flex items-center gap-1">
                  <select name="role" class="text-xs px-2 py-1.5 rounded-lg bg-gray-100 dark:bg-gray-800 border-0 focus:outline-none focus:ring-1 focus:ring-indigo-500">
                    <option value="user" {{ 'selected' if u.role == 'user' else '' }}>Người dùng</option>
                    <option value="developer" {{ 'selected' if u.role == 'developer' else '' }}>Developer</option>
                    {% if is_super_admin %}
                    <option value="admin" {{ 'selected' if u.role == 'admin' else '' }}>Admin</option>
                    {% endif %}
                  </select>
                  <button type="submit" class="text-xs font-semibold px-2.5 py-1.5 rounded-lg bg-indigo-50 dark:bg-indigo-900/30 text-indigo-600 dark:text-indigo-400 hover:bg-indigo-100">Cập nhật</button>
                </form>

                {% if u.role == 'user' %}
                <form method="POST" action="{{ url_for('developer_change_plan', user_id=u.id) }}" class="inline-flex items-center gap-1">
                  <select name="plan" class="text-xs px-2 py-1.5 rounded-lg bg-gray-100 dark:bg-gray-800 border-0 focus:outline-none focus:ring-1 focus:ring-indigo-500">
                    {% for p in plan_order %}
                    <option value="{{ p }}" {{ 'selected' if u.plan == p else '' }}>{{ plan_meta[p].icon }} {{ plan_meta[p].label }}</option>
                    {% endfor %}
                  </select>
                  <button type="submit" class="text-xs font-semibold px-2.5 py-1.5 rounded-lg bg-emerald-50 dark:bg-emerald-900/30 text-emerald-600 dark:text-emerald-400 hover:bg-emerald-100">Đổi gói</button>
                </form>
                {% endif %}

                <form method="POST" action="{{ url_for('developer_toggle_lock', user_id=u.id) }}" class="inline"
                  onsubmit="return {{ 'true' if u.is_locked else 'confirm(\'Khoá tài khoản này?\')' }};">
                  <button type="submit" class="text-xs font-semibold px-2.5 py-1.5 rounded-lg {{ 'bg-emerald-50 dark:bg-emerald-900/30 text-emerald-600 hover:bg-emerald-100' if u.is_locked else 'bg-amber-50 dark:bg-amber-900/30 text-amber-600 hover:bg-amber-100' }}">
                    {{ 'Mở khoá' if u.is_locked else 'Khoá' }}
                  </button>
                </form>

                <form method="POST" action="{{ url_for('developer_reset_session', user_id=u.id) }}" class="inline" onsubmit="return confirm('Đăng xuất mọi phiên đăng nhập của tài khoản này?');">
                  <button type="submit" class="text-xs font-semibold px-2.5 py-1.5 rounded-lg bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 hover:bg-gray-200">Reset session</button>
                </form>

                {% if u.role != 'admin' %}
                <form method="POST" action="{{ url_for('developer_delete_user', user_id=u.id) }}" class="inline" onsubmit="return confirm('XOÁ VĨNH VIỄN tài khoản này? Không thể hoàn tác.');">
                  <button type="submit" class="text-xs font-semibold px-2.5 py-1.5 rounded-lg bg-red-600 text-white hover:bg-red-700">Xoá</button>
                </form>
                {% endif %}
              </div>
              {% endif %}
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>

    <p class="text-center text-xs text-gray-400 pb-4">
      Trang này chỉ hiển thị số liệu tổng hợp (số lượt, độ dài, môn học, chế độ) — không lưu/hiển thị nội dung câu hỏi hay câu trả lời của học sinh.
    </p>
  </main>

  <script>
    function filterUserTable() {
      const q = document.getElementById('userSearchInput').value.trim().toLowerCase();
      document.querySelectorAll('#usersTableBody tr').forEach(row => {
        row.style.display = row.dataset.username.includes(q) ? '' : 'none';
      });
    }
  </script>
</body>
</html>
'''

# ==========================================
# 3.1 ĐĂNG NHẬP BẰNG GOOGLE — helper dùng chung
# ==========================================
def _slugify_username(base):
    """Chuyển email/tên thành username hợp lệ (chỉ chữ, số, gạch dưới, 3-32 ký tự)."""
    base = re.sub(r'[^A-Za-z0-9_]', '', (base or '').split('@')[0]) or 'user'
    base = base[:24] or 'user'
    if len(base) < 3:
        base = (base + '_user')[:24]
    return base


def get_or_create_oauth_user(provider, oauth_id, email, display_name):
    """Tìm tài khoản đã liên kết với (provider, oauth_id); nếu chưa có thì tạo mới.
    Không bao giờ lưu hay yêu cầu mật khẩu Google — chỉ nhận id/email/tên
    do chính Google xác thực và trả về qua OAuth, KHÔNG đụng tới thông tin
    đăng nhập thật của người dùng ở phía Google."""
    db = get_db()
    existing = db.execute(
        'SELECT * FROM users WHERE oauth_provider = ? AND oauth_id = ?', (provider, oauth_id)
    ).fetchone()
    if existing:
        return existing

    # Nếu email đã có tài khoản (đăng ký bằng mật khẩu trước đó) -> liên kết thêm OAuth vào đó
    if email:
        existing_email = db.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
        if existing_email:
            db.execute(
                'UPDATE users SET oauth_provider = ?, oauth_id = ? WHERE id = ?',
                (provider, oauth_id, existing_email['id'])
            )
            db.commit()
            return db.execute('SELECT * FROM users WHERE id = ?', (existing_email['id'],)).fetchone()

    base_username = _slugify_username(display_name or email or provider)
    username = base_username
    suffix = 0
    while db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone():
        suffix += 1
        username = f"{base_username}{suffix}"[:32]

    cur = db.execute(
        '''INSERT INTO users (username, password_hash, role, created_at, email, oauth_provider, oauth_id)
           VALUES (?, '', 'user', ?, ?, ?, ?)''',
        (username, now_iso(), email, provider, oauth_id)
    )
    db.commit()
    return db.execute('SELECT * FROM users WHERE id = ?', (cur.lastrowid,)).fetchone()


def _login_session_for(user):
    session.clear()
    session['user_id'] = user['id']
    session['username'] = user['username']
    session['session_version'] = user['session_version'] if 'session_version' in user.keys() else 0


@app.route('/auth/<provider>')
def oauth_start(provider):
    if provider != 'google' or not oauth or not hasattr(oauth, provider) or not google_login_effective_enabled():
        flash('Phương thức đăng nhập này hiện chưa được bật.')
        return redirect(url_for('login_page'))
    redirect_uri = url_for('oauth_callback', provider=provider, _external=True)
    client = getattr(oauth, provider)
    return client.authorize_redirect(redirect_uri)


@app.route('/auth/<provider>/callback')
def oauth_callback(provider):
    if provider != 'google' or not oauth or not hasattr(oauth, provider) or not google_login_effective_enabled():
        flash('Phương thức đăng nhập này hiện chưa được bật.')
        return redirect(url_for('login_page'))

    client = getattr(oauth, provider)
    try:
        token = client.authorize_access_token()
    except Exception:
        flash('Đăng nhập bị huỷ hoặc hết hạn phiên OAuth, em thử lại nhé.')
        return redirect(url_for('login_page'))

    try:
        userinfo = token.get('userinfo') or client.userinfo()
        oauth_id = userinfo.get('sub')
        email = userinfo.get('email')
        name = userinfo.get('name') or (email.split('@')[0] if email else 'google_user')
    except Exception:
        flash('Không lấy được thông tin tài khoản từ nhà cung cấp, em thử lại nhé.')
        return redirect(url_for('login_page'))

    if not oauth_id:
        flash('Đăng nhập không thành công, em thử lại nhé.')
        return redirect(url_for('login_page'))

    user = get_or_create_oauth_user(provider, str(oauth_id), email, name)
    if user['is_locked']:
        reason = (user['lock_reason'] or '').strip()
        flash('Tài khoản này đã bị khoá.' + (f' Lý do: {reason}' if reason else ''))
        return redirect(url_for('login_page'))
    _login_session_for(user)
    return redirect(url_for('home'))


# ==========================================
# 4. ĐỊNH TUYẾN TÀI KHOẢN (Đăng ký / Đăng nhập / Đăng xuất)
# ==========================================
def _auth_ctx(**extra):
    ctx = {'google_enabled': google_login_effective_enabled()}
    ctx.update(extra)
    return ctx


@app.route('/register', methods=['GET', 'POST'])
def register_page():
    if current_user_id():
        return redirect(url_for('home'))

    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        confirm = request.form.get('confirm') or ''

        if not USERNAME_RE.match(username):
            flash('Tên đăng nhập phải từ 3-32 ký tự, chỉ gồm chữ cái, số hoặc dấu gạch dưới.')
        elif len(password) < 6:
            flash('Mật khẩu phải có ít nhất 6 ký tự.')
        elif password != confirm:
            flash('Mật khẩu nhập lại không khớp.')
        else:
            db = get_db()
            existing = db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
            if existing:
                flash('Tên đăng nhập này đã được sử dụng.')
            else:
                pw_hash = generate_password_hash(password)
                cur = db.execute(
                    'INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)',
                    (username, pw_hash, now_iso())
                )
                db.commit()
                new_user = db.execute('SELECT * FROM users WHERE id = ?', (cur.lastrowid,)).fetchone()
                _login_session_for(new_user)
                return redirect(url_for('home'))

        return render_template_string(AUTH_HTML, mode='register', username=username, **_auth_ctx())

    return render_template_string(AUTH_HTML, mode='register', username='', **_auth_ctx())


@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if current_user_id():
        return redirect(url_for('home'))

    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''

        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()

        # Tài khoản tạo qua Google không có mật khẩu (password_hash rỗng)
        # -> không cho đăng nhập bằng form mật khẩu, hướng dẫn dùng lại nút OAuth.
        if user and not user['password_hash']:
            flash('Tài khoản này đăng nhập bằng Google. Vui lòng dùng nút tương ứng bên dưới.')
            return render_template_string(AUTH_HTML, mode='login', username=username, **_auth_ctx())

        if user and user['password_hash'] and check_password_hash(user['password_hash'], password):
            if user['is_locked']:
                reason = (user['lock_reason'] or '').strip()
                flash('Tài khoản này đã bị khoá.' + (f' Lý do: {reason}' if reason else ''))
                return render_template_string(AUTH_HTML, mode='login', username=username, **_auth_ctx())
            _login_session_for(user)
            return redirect(url_for('home'))

        flash('Tên đăng nhập hoặc mật khẩu không đúng.')
        return render_template_string(AUTH_HTML, mode='login', username=username, **_auth_ctx())

    return render_template_string(AUTH_HTML, mode='login', username='', **_auth_ctx())


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))


# ==========================================
# 5. ĐỊNH TUYẾN CHÍNH (Trang chủ gia sư AI)
# ==========================================
@app.route('/')
@login_required
def home():
    role = current_user_role()
    user = current_user()
    plan = effective_plan(user)
    unlocked_by_plan = {
        p: [k for k in THINKING_MODE_ORDER if thinking_mode_unlocked(k, p)] for p in PLAN_ORDER
    }
    # Ưu đãi lần đầu chỉ phụ thuộc số đơn ĐÃ TRẢ TIỀN của tài khoản (không phụ thuộc đang xem
    # gói nào), nên chỉ cần gọi 1 lần với 1 gói bất kỳ trong PLAN_PRICING để lấy is_discounted.
    discount_amounts = {}
    is_discount_eligible, discount_months_left = False, 0
    if user:
        for p, base in PLAN_PRICING.items():
            amount, base_amount, is_discounted, paid_count = compute_checkout_price(user['id'], p)
            discount_amounts[p] = amount
            is_discount_eligible = is_discounted
            discount_months_left = max(0, FIRST_TIME_DISCOUNT_MONTHS - paid_count)
    return render_template_string(
        HTML,
        username=session.get('username', ''),
        role=role,
        role_icon=role_meta(role)['icon'],
        role_label=role_meta(role)['label'],
        is_developer=(role_rank(role) >= role_rank('developer')),
        is_admin=(role_rank(role) >= role_rank('admin')),
        app_name=app_display_name(user),
        current_plan=plan,
        plan_order=PLAN_ORDER,
        plan_meta=PLAN_META,
        plan_limits=PLAN_LIMITS,
        is_plan_role_based=(role_rank(role) >= role_rank('developer')),
        thinking_modes=THINKING_MODES,
        thinking_mode_order=THINKING_MODE_ORDER,
        unlocked_thinking_modes=unlocked_by_plan[plan],
        unlocked_by_plan=unlocked_by_plan,
        plan_pricing=PLAN_PRICING,
        discount_amounts=discount_amounts,
        is_discount_eligible=is_discount_eligible,
        discount_pct=FIRST_TIME_DISCOUNT_PCT,
        discount_months_left=discount_months_left,
        vnpay_enabled=VNPAY_ENABLED,
        bank_transfer_enabled=BANK_TRANSFER_ENABLED,
        payment_methods_enabled=PAYMENT_METHODS_ENABLED,
        achievements_meta=ACHIEVEMENTS_META,
    )


# ==========================================
# 6. ĐỊNH TUYẾN BẢO MẬT (Trang báo cáo)
# ==========================================
@app.route('/security')
def security_report():
    return render_template_string(SECURITY_HTML)


# ==========================================
# 6.1 ĐỊNH TUYẾN THỐNG KÊ (Chỉ dành cho tài khoản developer)
# ==========================================
@app.route('/developer')
@developer_required
def developer_stats():
    db = get_db()

    total_users = db.execute('SELECT COUNT(*) c FROM users').fetchone()['c']
    total_usage = db.execute('SELECT COUNT(*) c FROM usage_logs').fetchone()['c']
    error_count = db.execute("SELECT COUNT(*) c FROM usage_logs WHERE status = 'error'").fetchone()['c']
    error_rate = round((error_count / total_usage) * 100, 1) if total_usage else 0

    seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    new_users_7d = db.execute(
        'SELECT COUNT(*) c FROM users WHERE created_at >= ?', (seven_days_ago,)
    ).fetchone()['c']
    usage_7d = db.execute(
        'SELECT COUNT(*) c FROM usage_logs WHERE created_at >= ?', (seven_days_ago,)
    ).fetchone()['c']
    avg_per_day_7d = round(usage_7d / 7, 1)

    today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    usage_today = db.execute(
        "SELECT COUNT(*) c FROM usage_logs WHERE substr(created_at, 1, 10) = ?", (today_str,)
    ).fetchone()['c']

    # Số lượt sử dụng theo từng ngày trong 14 ngày gần nhất (kể cả ngày = 0 lượt).
    rows = db.execute('''
        SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS c
        FROM usage_logs
        WHERE created_at >= ?
        GROUP BY day
    ''', ((datetime.now(timezone.utc) - timedelta(days=13)).isoformat(),)).fetchall()
    counts_by_day = {r['day']: r['c'] for r in rows}

    daily_counts = []
    for i in range(13, -1, -1):
        day = (datetime.now(timezone.utc) - timedelta(days=i)).strftime('%Y-%m-%d')
        daily_counts.append({'label': day[5:], 'count': counts_by_day.get(day, 0)})
    max_count = max((d['count'] for d in daily_counts), default=0)
    for d in daily_counts:
        d['height'] = round((d['count'] / max_count) * 100, 1) if max_count else 2

    # Phân bổ theo môn học.
    subj_rows = db.execute('''
        SELECT subject, COUNT(*) AS c FROM usage_logs
        WHERE subject IS NOT NULL AND subject != ''
        GROUP BY subject ORDER BY c DESC
    ''').fetchall()
    subject_stats = []
    for r in subj_rows:
        pct = round((r['c'] / total_usage) * 100, 1) if total_usage else 0
        subject_stats.append({'subject': r['subject'], 'count': r['c'], 'pct': pct})

    # Phân bổ theo chế độ học tập.
    mode_rows = db.execute('''
        SELECT mode, COUNT(*) AS c FROM usage_logs
        WHERE mode IS NOT NULL AND mode != ''
        GROUP BY mode ORDER BY c DESC
    ''').fetchall()
    mode_stats = []
    for r in mode_rows:
        pct = round((r['c'] / total_usage) * 100, 1) if total_usage else 0
        mode_stats.append({'mode': r['mode'], 'count': r['c'], 'pct': pct})

    # Top người dùng hoạt động nhiều nhất.
    top_rows = db.execute('''
        SELECT u.username AS username, u.role AS role,
               COUNT(l.id) AS usage_count, MAX(l.created_at) AS last_used
        FROM users u
        LEFT JOIN usage_logs l ON l.user_id = u.id
        GROUP BY u.id
        ORDER BY usage_count DESC, last_used DESC
        LIMIT 8
    ''').fetchall()
    top_users = []
    for r in top_rows:
        last_used = r['last_used']
        top_users.append({
            'username': r['username'],
            'role': r['role'],
            'usage_count': r['usage_count'],
            'last_used': last_used[:16].replace('T', ' ') if last_used else None,
        })

    all_users = db.execute(
        'SELECT id, username, role, plan, created_at, is_locked, lock_reason FROM users ORDER BY id ASC'
    ).fetchall()

    token_row = db.execute(
        'SELECT COALESCE(SUM(message_chars),0) AS mc, COALESCE(SUM(response_chars),0) AS rc FROM usage_logs'
    ).fetchone()
    estimated_tokens = round((token_row['mc'] + token_row['rc']) / 4)

    # ---- Đơn nâng cấp gói (thanh toán) ----
    pending_orders_rows = db.execute('''
        SELECT o.*, u.username AS username FROM payment_orders o
        LEFT JOIN users u ON u.id = o.user_id
        WHERE o.status = 'pending' ORDER BY o.created_at ASC
    ''').fetchall()
    recent_orders_rows = db.execute('''
        SELECT o.*, u.username AS username FROM payment_orders o
        LEFT JOIN users u ON u.id = o.user_id
        ORDER BY o.created_at DESC LIMIT 20
    ''').fetchall()
    total_revenue = db.execute(
        "SELECT COALESCE(SUM(amount), 0) r FROM payment_orders WHERE status = 'paid'"
    ).fetchone()['r']

    # ---- Báo lỗi từ học sinh ----
    open_issues_count = db.execute("SELECT COUNT(*) c FROM issue_reports WHERE status = 'open'").fetchone()['c']
    issue_rows = db.execute('''
        SELECT r.*, u.username AS username FROM issue_reports r
        LEFT JOIN users u ON u.id = r.user_id
        ORDER BY (r.status = 'open') DESC, r.created_at DESC LIMIT 30
    ''').fetchall()

    role = current_user_role()

    return render_template_string(
        DEV_STATS_HTML,
        total_users=total_users,
        new_users_7d=new_users_7d,
        total_usage=total_usage,
        usage_today=usage_today,
        usage_7d=usage_7d,
        avg_per_day_7d=avg_per_day_7d,
        error_rate=error_rate,
        error_count=error_count,
        estimated_tokens=estimated_tokens,
        daily_counts=daily_counts,
        subject_stats=subject_stats,
        mode_stats=mode_stats,
        top_users=top_users,
        all_users=[dict(u, effective_plan=effective_plan(u), plan_is_role_based=(role_rank(u['role']) >= role_rank('developer'))) for u in all_users],
        role_meta=ROLE_META,
        role_rank_map={r: role_rank(r) for r in ROLE_ORDER},
        plan_meta=PLAN_META,
        plan_order=PLAN_ORDER,
        plan_pricing=PLAN_PRICING,
        current_role=role,
        is_admin=(role_rank(role) >= role_rank('admin')),
        is_super_admin=(role_rank(role) >= role_rank('super_admin')),
        current_user_id_val=current_user_id(),
        banner_message=get_setting('banner_message', '') or '',
        google_configured=bool(GOOGLE_OAUTH_ENABLED),
        google_override=get_setting('google_login_override', ''),
        maintenance_mode=(get_setting('maintenance_mode', 'off') == 'on'),
        ai_model_override=get_setting('ai_model_override', '') or '',
        ai_temperature_override=get_setting('ai_temperature_override', '') or '',
        global_system_addendum=get_setting('global_system_addendum', '') or '',
        default_model=CONSOLEX_MODEL,
        pending_orders=[dict(o) for o in pending_orders_rows],
        recent_orders=[dict(o) for o in recent_orders_rows],
        total_revenue=total_revenue,
        vnpay_enabled=VNPAY_ENABLED,
        bank_transfer_enabled=BANK_TRANSFER_ENABLED,
        issue_reports=[dict(r) for r in issue_rows],
        open_issues_count=open_issues_count,
    )


@app.route('/developer/users/<int:user_id>/role', methods=['POST'])
@admin_required
def developer_change_role(user_id):
    """Đổi vai trò 1 tài khoản. Admin chỉ đổi qua lại User<->Developer; chỉ Super Admin mới
    được cấp/thu hồi Admin trở lên. Luôn giữ lại ít nhất 1 tài khoản Admin/Super Admin."""
    db = get_db()
    target = db.execute('SELECT id, role, username FROM users WHERE id = ?', (user_id,)).fetchone()
    if not target:
        flash('Không tìm thấy tài khoản.')
        return redirect(url_for('developer_stats'))

    new_role = (request.form.get('role') or '').strip()
    actor = current_user()
    ok, err = can_manage_role(actor['role'], target['role'], new_role)
    if not ok:
        flash(err)
        return redirect(url_for('developer_stats'))

    if role_rank(target['role']) >= role_rank('admin') and role_rank(new_role) < role_rank('admin'):
        remaining = db.execute(
            "SELECT COUNT(*) c FROM users WHERE role IN ('admin','super_admin') AND id != ?", (user_id,)
        ).fetchone()['c']
        if remaining == 0:
            flash('Không thể hạ quyền — đây là tài khoản Admin/Super Admin cuối cùng của hệ thống.')
            return redirect(url_for('developer_stats'))

    db.execute('UPDATE users SET role = ? WHERE id = ?', (new_role, user_id))
    db.commit()
    write_audit('change_role', target['username'], f"{target['role']} → {new_role}")
    flash(f"Đã đổi vai trò của '{target['username']}' thành {role_meta(new_role)['label']}.")
    return redirect(url_for('developer_stats'))


@app.route('/developer/users/<int:user_id>/plan', methods=['POST'])
@admin_required
def developer_change_plan(user_id):
    """Gán gói Free/Premium/Max cho 1 tài khoản (không có cổng thanh toán thật — đây là cách
    Admin "nâng cấp" thủ công cho học sinh). Không áp dụng cho Developer trở lên vì các vai
    trò đó đã luôn có Max vô điều kiện (xem effective_plan())."""
    db = get_db()
    target = db.execute('SELECT id, role, username, plan FROM users WHERE id = ?', (user_id,)).fetchone()
    if not target:
        flash('Không tìm thấy tài khoản.')
        return redirect(url_for('developer_stats'))

    if role_rank(target['role']) >= role_rank('developer'):
        flash(f"'{target['username']}' đã tự động có gói Max theo vai trò, không cần đổi gói.")
        return redirect(url_for('developer_stats'))

    new_plan = (request.form.get('plan') or '').strip()
    if new_plan not in PLAN_ORDER:
        flash('Gói không hợp lệ.')
        return redirect(url_for('developer_stats'))

    if new_plan == 'free':
        # Hạ về Free thì xoá luôn hạn dùng — không cần grant_plan_upgrade() (hàm đó chỉ dùng để
        # CẤP gói có phí theo tháng, hạ về Free không có khái niệm "hạn dùng").
        db.execute('UPDATE users SET plan = ?, plan_expires_at = NULL WHERE id = ?', (new_plan, user_id))
        db.commit()
        write_audit('change_plan', target['username'], f"{target['plan']} → {new_plan}")
    else:
        # Admin "tặng" gói: CHỈ 1 THÁNG miễn phí (không phải vĩnh viễn) — dùng chung
        # grant_plan_upgrade() với cổng thanh toán thật để hạn dùng được tính nhất quán và tự
        # rơi về Free khi hết hạn (xem effective_plan()).
        grant_plan_upgrade(user_id, new_plan, order_code=f"gift_by_{session.get('username','admin')}",
                            actor=session.get('username', 'admin'), months=1)
    flash(f"Đã đổi gói của '{target['username']}' thành {plan_meta(new_plan)['label']}"
          + ('' if new_plan == 'free' else ' (tặng miễn phí 1 tháng).'))
    return redirect(url_for('developer_stats'))


@app.route('/developer/payments/<order_code>/confirm', methods=['POST'])
@admin_required
def developer_confirm_payment(order_code):
    """Admin bấm xác nhận ĐÃ NHẬN ĐƯỢC TIỀN cho 1 đơn chuyển khoản VietQR (thủ công, vì app
    không có quyền đọc sao kê ngân hàng tự động). Đơn qua VNPAY thì KHÔNG cần bấm tay — đã tự
    chốt qua IPN (xem vnpay_ipn())."""
    db = get_db()
    order = db.execute('SELECT * FROM payment_orders WHERE order_code = ?', (order_code,)).fetchone()
    if not order:
        flash('Không tìm thấy đơn hàng.')
        return redirect(url_for('developer_stats'))
    if order['status'] == 'paid':
        flash('Đơn này đã được xác nhận trước đó.')
        return redirect(url_for('developer_stats'))

    db.execute("UPDATE payment_orders SET status = 'paid', paid_at = ? WHERE order_code = ?",
               (now_iso(), order_code))
    db.commit()
    grant_plan_upgrade(order['user_id'], order['plan'], order_code, actor=session.get('username', ''))
    flash(f"Đã xác nhận thanh toán & nâng cấp gói cho đơn {order_code}.")
    return redirect(url_for('developer_stats'))


@app.route('/developer/payments/<order_code>/cancel', methods=['POST'])
@admin_required
def developer_cancel_payment(order_code):
    db = get_db()
    order = db.execute('SELECT * FROM payment_orders WHERE order_code = ?', (order_code,)).fetchone()
    if not order:
        flash('Không tìm thấy đơn hàng.')
        return redirect(url_for('developer_stats'))
    db.execute("UPDATE payment_orders SET status = 'cancelled' WHERE order_code = ?", (order_code,))
    db.commit()
    write_audit('cancel_payment_order', target=order_code)
    flash(f"Đã huỷ đơn {order_code}.")
    return redirect(url_for('developer_stats'))


@app.route('/developer/issues/<int:issue_id>/resolve', methods=['POST'])
@admin_required
def developer_resolve_issue(issue_id):
    """Đánh dấu 1 báo cáo lỗi là đã xử lý (hoặc mở lại nếu bấm lần nữa)."""
    db = get_db()
    row = db.execute('SELECT id, status FROM issue_reports WHERE id = ?', (issue_id,)).fetchone()
    if not row:
        flash('Không tìm thấy báo cáo này.')
        return redirect(url_for('developer_stats'))
    new_status = 'open' if row['status'] == 'resolved' else 'resolved'
    resolved_at = now_iso() if new_status == 'resolved' else None
    db.execute('UPDATE issue_reports SET status = ?, resolved_at = ?, resolved_by = ? WHERE id = ?',
               (new_status, resolved_at, session.get('username', '') if new_status == 'resolved' else None, issue_id))
    db.commit()
    if new_status == 'resolved':
        write_audit('resolve_issue_report', target=str(issue_id))
    return redirect(url_for('developer_stats'))


@app.route('/developer/users/<int:user_id>/lock', methods=['POST'])
@admin_required
def developer_toggle_lock(user_id):
    """Khoá / mở khoá tài khoản. Chỉ Super Admin được khoá tài khoản Admin/Super Admin."""
    db = get_db()
    target = db.execute('SELECT id, role, username, is_locked FROM users WHERE id = ?', (user_id,)).fetchone()
    if not target:
        flash('Không tìm thấy tài khoản.')
        return redirect(url_for('developer_stats'))

    actor = current_user()
    if actor['role'] != 'super_admin' and role_rank(target['role']) >= role_rank('admin'):
        flash('Chỉ Super Admin mới có thể khoá tài khoản Admin/Super Admin.')
        return redirect(url_for('developer_stats'))
    if target['id'] == current_user_id():
        flash('Không thể tự khoá tài khoản của chính mình.')
        return redirect(url_for('developer_stats'))

    new_locked = 0 if target['is_locked'] else 1
    reason = (request.form.get('reason') or '').strip()[:200] if new_locked else ''
    db.execute('UPDATE users SET is_locked = ?, lock_reason = ? WHERE id = ?', (new_locked, reason, user_id))
    db.commit()
    write_audit('lock' if new_locked else 'unlock', target['username'], reason)
    flash(('Đã khoá' if new_locked else 'Đã mở khoá') + f" tài khoản '{target['username']}'.")
    return redirect(url_for('developer_stats'))


@app.route('/developer/users/<int:user_id>/reset-session', methods=['POST'])
@admin_required
def developer_reset_session(user_id):
    """Đăng xuất tài khoản này khỏi TẤT CẢ thiết bị đang đăng nhập (tăng session_version)."""
    db = get_db()
    target = db.execute('SELECT id, role, username FROM users WHERE id = ?', (user_id,)).fetchone()
    if not target:
        flash('Không tìm thấy tài khoản.')
        return redirect(url_for('developer_stats'))

    actor = current_user()
    if actor['role'] != 'super_admin' and role_rank(target['role']) >= role_rank('admin'):
        flash('Chỉ Super Admin mới có thể reset session của tài khoản Admin/Super Admin.')
        return redirect(url_for('developer_stats'))

    db.execute('UPDATE users SET session_version = session_version + 1 WHERE id = ?', (user_id,))
    db.commit()
    write_audit('reset_session', target['username'])
    flash(f"Đã đăng xuất toàn bộ phiên đăng nhập của '{target['username']}'.")
    return redirect(url_for('developer_stats'))


@app.route('/developer/users/<int:user_id>/delete', methods=['POST'])
@admin_required
def developer_delete_user(user_id):
    """Xoá tài khoản + toàn bộ dữ liệu liên quan. Không cho xoá Admin/Super Admin qua UI này
    (an toàn hệ thống) và không cho tự xoá chính mình."""
    db = get_db()
    target = db.execute('SELECT id, role, username FROM users WHERE id = ?', (user_id,)).fetchone()
    if not target:
        flash('Không tìm thấy tài khoản.')
        return redirect(url_for('developer_stats'))
    if role_rank(target['role']) >= role_rank('admin'):
        flash('Không thể xoá tài khoản Admin/Super Admin qua giao diện này.')
        return redirect(url_for('developer_stats'))
    if target['id'] == current_user_id():
        flash('Không thể tự xoá tài khoản của chính mình.')
        return redirect(url_for('developer_stats'))

    conv_ids = [r['id'] for r in db.execute(
        'SELECT id FROM conversations WHERE user_id = ?', (user_id,)
    ).fetchall()]
    if conv_ids:
        placeholders = ','.join('?' * len(conv_ids))
        db.execute(f'DELETE FROM messages WHERE conversation_id IN ({placeholders})', conv_ids)
    db.execute('DELETE FROM conversations WHERE user_id = ?', (user_id,))
    db.execute('DELETE FROM projects WHERE user_id = ?', (user_id,))
    db.execute('DELETE FROM custom_tutors WHERE owner_id = ?', (user_id,))
    db.execute('DELETE FROM api_keys WHERE user_id = ?', (user_id,))
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()
    write_audit('delete_account', target['username'])
    flash(f"Đã xoá tài khoản '{target['username']}'.")
    return redirect(url_for('developer_stats'))


@app.route('/developer/banner', methods=['POST'])
@admin_required
def developer_set_banner():
    message = (request.form.get('banner_message') or '').strip()[:300]
    set_setting('banner_message', message)
    write_audit('set_banner', detail=message)
    flash('Đã cập nhật thông báo hệ thống.' if message else 'Đã xoá thông báo hệ thống.')
    return redirect(url_for('developer_stats'))


@app.route('/developer/maintenance', methods=['POST'])
@admin_required
def developer_toggle_maintenance():
    """Chế độ bảo trì: chặn học sinh thường gửi câu hỏi AI, Admin/Super Admin vẫn dùng được
    bình thường để kiểm tra hệ thống trước khi mở lại cho tất cả."""
    value = 'on' if (request.form.get('value') == 'on') else 'off'
    set_setting('maintenance_mode', value)
    write_audit('toggle_maintenance', detail=value)
    flash('Đã bật chế độ BẢO TRÌ — học sinh tạm thời không gửi được câu hỏi.' if value == 'on'
          else 'Đã tắt chế độ bảo trì.')
    return redirect(url_for('developer_stats'))


@app.route('/developer/ai-config', methods=['POST'])
@admin_required
def developer_ai_config():
    """Ghi đè model / temperature / hướng dẫn hệ thống chung mà KHÔNG cần sửa .env hay restart."""
    model_override = (request.form.get('model_override') or '').strip()[:80]
    temp_raw = (request.form.get('temperature_override') or '').strip()
    addendum = (request.form.get('system_addendum') or '').strip()[:2000]

    set_setting('ai_model_override', model_override)
    set_setting('global_system_addendum', addendum)
    try:
        if temp_raw:
            t = max(0.0, min(1.0, float(temp_raw)))
            set_setting('ai_temperature_override', str(t))
        else:
            set_setting('ai_temperature_override', '')
    except ValueError:
        flash('Giá trị temperature không hợp lệ (phải là số từ 0 đến 1).')
        return redirect(url_for('developer_stats'))

    write_audit('update_ai_config', detail=f"model={model_override or '(mặc định)'}; temp={temp_raw or '(mặc định)'}")
    flash('Đã cập nhật cấu hình AI.')
    return redirect(url_for('developer_stats'))


@app.route('/developer/google-login', methods=['POST'])
@admin_required
def developer_toggle_google_login():
    value = (request.form.get('value') or '').strip()
    if value not in ('on', 'off', ''):
        value = ''
    set_setting('google_login_override', value)
    write_audit('toggle_google_login', detail=value or '(theo .env)')
    flash('Đã cập nhật trạng thái đăng nhập Google.')
    return redirect(url_for('developer_stats'))


@app.route('/developer/export.csv')
@admin_required
def developer_export_csv():
    """Xuất toàn bộ usage_logs ra CSV (chỉ số liệu tổng hợp, không có nội dung câu hỏi/trả lời)."""
    db = get_db()
    rows = db.execute('SELECT * FROM usage_logs ORDER BY id DESC').fetchall()
    header = ['id', 'user_id', 'endpoint', 'subject', 'mode', 'message_chars',
              'response_chars', 'had_file', 'had_image', 'status', 'created_at']

    def _csv_field(value):
        s = '' if value is None else str(value)
        if any(c in s for c in (',', '"', '\n')):
            s = '"' + s.replace('"', '""') + '"'
        return s

    def generate_csv():
        yield ','.join(header) + '\n'
        for r in rows:
            yield ','.join(_csv_field(r[h]) for h in header) + '\n'

    return Response(
        generate_csv(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=studymate_usage_logs.csv'},
    )


AUDIT_LOG_HTML = r'''
<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Nhật ký hệ thống - StudyMate AI Max</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>tailwind.config = { darkMode: 'class' };</script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.6.0/css/all.min.css">
</head>
<body class="bg-[#0f0f0f] text-gray-200 min-h-screen">
  <nav class="sticky top-0 z-10 bg-[#0f0f0f]/90 backdrop-blur border-b border-gray-800 px-4 sm:px-6 py-3 flex items-center justify-between">
    <div class="flex items-center gap-2 font-bold"><i class="fas fa-scroll text-red-400"></i> Nhật ký hệ thống (Super Admin)</div>
    <a href="/developer" class="text-sm text-indigo-400 hover:underline"><i class="fas fa-arrow-left mr-1"></i>Về Dashboard</a>
  </nav>
  <main class="max-w-5xl mx-auto px-4 sm:px-6 py-6">
    <p class="text-xs text-gray-500 mb-4">Ghi lại các thao tác nhạy cảm: đổi vai trò, khoá/mở khoá, xoá tài khoản, reset session, cấu hình hệ thống. 200 dòng gần nhất.</p>
    <div class="bg-[#1a1a1a] rounded-2xl border border-gray-800 overflow-x-auto">
      <table class="w-full text-sm min-w-[640px]">
        <thead>
          <tr class="text-left text-gray-500 text-xs uppercase border-b border-gray-800">
            <th class="py-2.5 px-4">Thời gian</th>
            <th class="py-2.5 px-4">Người thực hiện</th>
            <th class="py-2.5 px-4">Hành động</th>
            <th class="py-2.5 px-4">Đối tượng</th>
            <th class="py-2.5 px-4">Chi tiết</th>
          </tr>
        </thead>
        <tbody>
          {% for log in logs %}
          <tr class="border-b border-gray-900">
            <td class="py-2.5 px-4 text-gray-500 whitespace-nowrap">{{ log.created_at[:16].replace('T',' ') }}</td>
            <td class="py-2.5 px-4 font-medium">{{ log.actor_username or '(hệ thống)' }}</td>
            <td class="py-2.5 px-4"><span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-gray-800">{{ log.action }}</span></td>
            <td class="py-2.5 px-4 text-gray-400">{{ log.target }}</td>
            <td class="py-2.5 px-4 text-gray-500">{{ log.detail }}</td>
          </tr>
          {% else %}
          <tr><td colspan="5" class="py-8 text-center text-gray-500">Chưa có nhật ký nào.</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </main>
</body>
</html>
'''


@app.route('/developer/audit')
@super_admin_required
def developer_audit_log():
    db = get_db()
    logs = db.execute('SELECT * FROM audit_logs ORDER BY id DESC LIMIT 200').fetchall()
    return render_template_string(AUDIT_LOG_HTML, logs=[dict(l) for l in logs])


# ==========================================
# 7. GỌI API AI (xAI / Consolex-compatible) — STREAMING
# ==========================================
def stream_consolex_ai(system_prompt: str, user_content, max_tokens: int = 800):
    """Gọi xAI API ở chế độ stream=True và yield từng đoạn token nhận được.

    Dùng SESSION (requests.Session) để tái sử dụng kết nối TCP/TLS,
    giúp giảm độ trễ so với việc tạo kết nối mới mỗi lần gọi.

    `max_tokens` thay đổi theo "Chế độ suy nghĩ" đang chọn (Trợ Lý/Học Giả/Giáo Sư/Thiên Tài)
    — chế độ càng sâu thì ngân sách token càng cao để AI có "chỗ" suy luận/giải thích kỹ hơn.
    """
    if not XAI_API_KEY:
        raise RuntimeError("Thiếu XAI_API_KEY. Vui lòng thiết lập biến môi trường trước khi chạy server.")

    url = f"{CONSOLEX_API_BASE.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {XAI_API_KEY}",
        "Content-Type": "application/json",
    }

    # Admin/Super Admin có thể ghi đè model & temperature từ /developer mà không cần sửa .env
    # hay khởi động lại server (đọc từ bảng settings, có cache 1 request qua get_setting -> get_db/g).
    model = get_setting('ai_model_override', '') or CONSOLEX_MODEL
    temp_override = get_setting('ai_temperature_override', '')
    try:
        temperature = float(temp_override) if temp_override else 0.7
    except ValueError:
        temperature = 0.7

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }

    with SESSION.post(url, headers=headers, json=payload, timeout=60, stream=True) as resp:
        resp.raise_for_status()
        # QUAN TRỌNG: xAI trả JSON dạng UTF-8, nhưng header Content-Type của response
        # streaming thường không khai báo rõ "charset=utf-8". Khi đó thư viện `requests`
        # tự mặc định resp.encoding = 'ISO-8859-1' (theo chuẩn HTTP cũ cho text/*),
        # khiến decode_unicode=True bên dưới đọc sai từng byte UTF-8 của tiếng Việt
        # -> ra ký tự lạ kiểu "Æ¡", "áº§"... Ép rõ encoding='utf-8' để sửa tận gốc lỗi này.
        resp.encoding = 'utf-8'
        for raw_line in resp.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            if not raw_line.startswith("data: "):
                continue
            data_str = raw_line[len("data: "):].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                delta = chunk["choices"][0]["delta"].get("content")
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
            if delta:
                yield delta


# ==========================================
# 8. UPLOAD FILE / ẢNH
# ==========================================
def _truncate_text(full_text: str, text_limit=None) -> str:
    if text_limit is None:
        return full_text
    truncated = full_text[:text_limit]
    if len(full_text) > text_limit:
        truncated += "\n\n[... nội dung bị cắt bớt do quá dài — nâng cấp gói để trích xuất được nhiều hơn ...]"
    return truncated


def handle_pdf_upload(raw, text_limit=None):
    try:
        reader = PdfReader(io.BytesIO(raw))
        num_pages = len(reader.pages)
        text_parts = []
        for page in reader.pages:
            page_text = page.extract_text() or ""
            if page_text.strip():
                text_parts.append(page_text)
        full_text = "\n\n".join(text_parts).strip()

        if not full_text:
            return jsonify({
                "error": "File PDF này có vẻ là bản scan/ảnh nên chưa trích được chữ. "
                         "Em thử gõ trực tiếp câu hỏi hoặc nội dung cần hỏi nhé!"
            }), 200

        return jsonify({"text": _truncate_text(full_text, text_limit), "pages": num_pages})
    except Exception as e:
        return jsonify({"error": f"Không đọc được file PDF: {e}"}), 500


def handle_docx_upload(raw, text_limit=None):
    if docx_lib is None:
        return jsonify({
            "error": "Server chưa cài thư viện đọc Word. Vui lòng chạy: pip install python-docx"
        }), 500
    try:
        document = docx_lib.Document(io.BytesIO(raw))
        paragraphs = [p.text for p in document.paragraphs if p.text.strip()]
        # Cũng lấy nội dung trong các bảng (table) nếu có.
        for table in document.tables:
            for row in table.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_text:
                    paragraphs.append(row_text)

        full_text = "\n".join(paragraphs).strip()
        if not full_text:
            return jsonify({"error": "File Word này không có nội dung văn bản để đọc."}), 200

        return jsonify({"text": _truncate_text(full_text, text_limit), "pages": None})
    except Exception as e:
        return jsonify({"error": f"Không đọc được file Word: {e}"}), 500


def handle_text_upload(raw, text_limit=None):
    try:
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            text = raw.decode('utf-8', errors='ignore')
        text = text.strip()

        if not text:
            return jsonify({"error": "File này không có nội dung."}), 200

        return jsonify({"text": _truncate_text(text, text_limit), "pages": None})
    except Exception as e:
        return jsonify({"error": f"Không đọc được file: {e}"}), 500


def handle_image_upload(raw, filename, ext):
    # Dung lượng đã được kiểm tra theo gói (Free/Premium/Max) ở route /api/upload trước khi
    # gọi tới đây, nên không cần kiểm tra lại giới hạn cứng ở bước này nữa.
    mime_map = {
        '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.gif': 'image/gif', '.webp': 'image/webp',
    }
    mime = mime_map.get(ext, 'image/jpeg')

    # Nếu có Pillow: thu nhỏ ảnh quá lớn để giảm dung lượng gửi lên AI -> phản hồi nhanh hơn.
    if Image is not None:
        try:
            img = Image.open(io.BytesIO(raw))
            img.load()
            if max(img.size) > MAX_IMAGE_DIMENSION:
                ratio = MAX_IMAGE_DIMENSION / max(img.size)
                new_size = (max(1, int(img.size[0] * ratio)), max(1, int(img.size[1] * ratio)))
                img = img.resize(new_size, Image.LANCZOS)

            buf = io.BytesIO()
            if mime == 'image/gif':
                # Giữ nguyên GIF (kể cả animation) thay vì convert.
                img.save(buf, format='GIF')
            elif img.mode in ('RGBA', 'P', 'LA'):
                img = img.convert('RGB')
                img.save(buf, format='JPEG', quality=85)
                mime = 'image/jpeg'
            else:
                img.save(buf, format='JPEG', quality=85)
                mime = 'image/jpeg'
            raw = buf.getvalue()
        except Exception:
            pass  # nếu xử lý ảnh lỗi, dùng bytes gốc

    b64 = base64.b64encode(raw).decode('utf-8')
    data_url = f"data:{mime};base64,{b64}"
    return jsonify({"type": "image", "dataUrl": data_url, "name": filename})


def _uploads_used_last_24h(user_id):
    window_start = (datetime.now(timezone.utc) - timedelta(hours=UPLOAD_QUOTA_WINDOW_HOURS)).isoformat()
    return get_db().execute(
        'SELECT COUNT(*) c FROM file_uploads WHERE user_id = ? AND created_at >= ?',
        (user_id, window_start)
    ).fetchone()['c']


@app.route('/api/upload', methods=['POST'])
@login_required
def upload_file():
    if 'file' not in request.files:
        return jsonify({"error": "Không tìm thấy file trong yêu cầu."}), 400

    f = request.files['file']
    filename = f.filename or ''
    ext = os.path.splitext(filename.lower())[1]

    if ext not in ALLOWED_IMAGE_EXT and ext not in ALLOWED_DOC_EXT:
        return jsonify({
            "error": f"Định dạng {ext or 'không xác định'} chưa được hỗ trợ. "
                     "Em thử PDF, Word (.docx), .txt, .csv hoặc ảnh (PNG/JPG/GIF/WEBP) nhé!"
        }), 400

    user = current_user()
    plan = effective_plan(user)
    limits = plan_limits(plan)
    label = plan_meta(plan)['label']

    # 1) Giới hạn dung lượng MỖI file/ảnh theo gói (Free ≤20MB, Premium ≤500MB, Max ≤1GB).
    raw = f.read()
    size_mb = len(raw) / (1024 * 1024)
    if size_mb > limits['max_file_mb']:
        return jsonify({
            "error": f"File/ảnh này khoảng {size_mb:.1f}MB, vượt quá giới hạn {limits['max_file_mb']}MB "
                     f"của gói {label}. " + ("Em thử file nhỏ hơn nhé!" if plan == 'max'
                                              else "Em thử file nhỏ hơn hoặc nâng cấp gói nhé!")
        }), 400

    # 2) Giới hạn SỐ LƯỢT tải file/ảnh trong 24h gần nhất theo gói (Free: 20, Premium: 50,
    #    Max: không giới hạn). Đếm theo cửa sổ trượt 24h, tự "làm mới" dần theo thời gian.
    if limits['daily_uploads'] is not None:
        used = _uploads_used_last_24h(user['id'])
        if used >= limits['daily_uploads']:
            return jsonify({
                "error": f"Em đã dùng hết {limits['daily_uploads']} lượt tải file/ảnh trong 24h qua "
                         f"(gói {label}). Giới hạn sẽ tự làm mới dần trong vòng 24h tới, hoặc nâng cấp "
                         "gói để có thêm lượt tải nhé!"
            }), 429

    kind = 'image' if ext in ALLOWED_IMAGE_EXT else 'file'
    db = get_db()
    db.execute(
        'INSERT INTO file_uploads (user_id, kind, size_bytes, created_at) VALUES (?, ?, ?, ?)',
        (user['id'], kind, len(raw), now_iso())
    )
    db.commit()

    if ext in ALLOWED_IMAGE_EXT:
        return handle_image_upload(raw, filename, ext)
    if ext == '.pdf':
        return handle_pdf_upload(raw, limits['text_chars'])
    if ext == '.docx':
        return handle_docx_upload(raw, limits['text_chars'])
    return handle_text_upload(raw, limits['text_chars'])


@app.route('/api/plan', methods=['GET'])
@login_required
def api_plan():
    """Thông tin gói hiện tại + số lượt tải file/ảnh đã dùng trong 24h — dùng để hiển thị
    ở màn hình Cài đặt và hộp thoại Nâng cấp gói phía client."""
    user = current_user()
    plan = effective_plan(user)
    limits = plan_limits(plan)
    used = _uploads_used_last_24h(user['id']) if limits['daily_uploads'] is not None else 0
    is_role_based = role_rank(user['role']) >= role_rank('developer')

    days_remaining = None
    expires_at_iso = None
    if plan != 'free' and not is_role_based:
        try:
            raw = user['plan_expires_at']
            if raw:
                exp_dt = datetime.fromisoformat(raw)
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                days_remaining = max(0, (exp_dt - datetime.now(timezone.utc)).days)
                expires_at_iso = exp_dt.isoformat()
        except Exception:
            pass

    _, _, is_discount_eligible, paid_months = compute_checkout_price(user['id'], 'premium')

    return jsonify({
        'plan': plan,
        'label': plan_meta(plan)['label'],
        'icon': plan_meta(plan)['icon'],
        'is_role_based': is_role_based,
        'daily_upload_limit': limits['daily_uploads'],
        'daily_uploads_used': used,
        'max_file_mb': limits['max_file_mb'],
        'unlocked_thinking_modes': [
            k for k in THINKING_MODE_ORDER if thinking_mode_unlocked(k, plan)
        ],
        'plan_expires_at': expires_at_iso,
        'days_remaining': days_remaining,
        'is_discount_eligible': is_discount_eligible,
        'discount_pct': FIRST_TIME_DISCOUNT_PCT,
        'discount_months_used': paid_months,
        'discount_months_total': FIRST_TIME_DISCOUNT_MONTHS,
    })


@app.route('/api/gamification', methods=['GET'])
@login_required
def api_gamification():
    """XP / streak / thành tựu của tài khoản đang đăng nhập — hiển thị ở sidebar + Cài đặt."""
    return jsonify(get_user_stats(current_user_id()))


@app.route('/api/memories', methods=['GET'])
@login_required
def api_list_memories():
    conn = open_write_db()
    try:
        rows = conn.execute(
            'SELECT id, content, category, source, created_at FROM memories WHERE user_id = ? ORDER BY created_at DESC',
            (current_user_id(),)
        ).fetchall()
    finally:
        conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/memories', methods=['DELETE'])
@login_required
def api_clear_memories():
    """Xoá toàn bộ 'bộ nhớ' AI của chính học sinh này (quyền riêng tư — mỗi người chỉ xoá
    được bộ nhớ của mình)."""
    conn = open_write_db()
    try:
        conn.execute('DELETE FROM memories WHERE user_id = ?', (current_user_id(),))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"success": True})


@app.route('/api/report-issue', methods=['POST'])
@login_required
def api_report_issue():
    """Học sinh báo lỗi 1 câu trả lời cụ thể (hoặc báo lỗi chung). Lưu lại để Admin xem và
    xử lý ở trang /developer."""
    data = request.get_json(silent=True) or {}
    description = (data.get('description') or '').strip()
    message_excerpt = (data.get('messageExcerpt') or '').strip()[:2000]
    raw_conv_id = data.get('conversationId')

    if not description:
        return jsonify({"error": "Em mô tả lỗi cụ thể giúp Thầy/Cô nhé."}), 400
    if len(description) > 1000:
        return jsonify({"error": "Mô tả hơi dài, em rút gọn lại giúp Thầy/Cô nhé!"}), 400

    conv_id = None
    if raw_conv_id is not None:
        try:
            conv_id = int(raw_conv_id)
        except (TypeError, ValueError):
            conv_id = None

    db = get_db()
    db.execute(
        '''INSERT INTO issue_reports
           (user_id, conversation_id, message_excerpt, description, status, created_at)
           VALUES (?, ?, ?, ?, 'open', ?)''',
        (current_user_id(), conv_id, message_excerpt, description, now_iso())
    )
    db.commit()
    return jsonify({"success": True})


# ==========================================
# 8.5 THANH TOÁN NÂNG CẤP GÓI (VNPAY + Chuyển khoản VietQR)
# ==========================================
@app.route('/api/checkout', methods=['POST'])
@login_required
def api_checkout():
    """Tạo 1 đơn nâng cấp gói THEO THÁNG. method = 'vnpay' (thẻ ATM/Visa/Mastercard/JCB,
    redirect sang VNPAY) hoặc 'bank_transfer' (quét mã VietQR, Admin xác nhận thủ công sau
    khi nhận tiền). Tự áp dụng ưu đãi lần đầu (xem compute_checkout_price())."""
    user = current_user()
    role = current_user_role()
    if role_rank(role) >= role_rank('developer'):
        return jsonify({"error": "Tài khoản của em đã có gói Max theo vai trò, không cần nâng cấp."}), 400

    data = request.get_json(silent=True) or {}
    plan = (data.get('plan') or '').strip()
    method = (data.get('method') or '').strip()

    if plan not in PLAN_PRICING:
        return jsonify({"error": "Gói không hợp lệ."}), 400
    if plan_rank(plan) < plan_rank(effective_plan(user)):
        return jsonify({"error": "Không thể hạ xuống gói thấp hơn gói đang dùng qua đây."}), 400
    if method not in ('vnpay', 'bank_transfer'):
        return jsonify({"error": "Phương thức thanh toán không hợp lệ."}), 400
    if method == 'vnpay' and not VNPAY_ENABLED:
        return jsonify({"error": "Thanh toán qua thẻ (VNPAY) hiện chưa khả dụng."}), 400
    if method == 'bank_transfer' and not BANK_TRANSFER_ENABLED:
        return jsonify({"error": "Thanh toán chuyển khoản hiện chưa khả dụng."}), 400

    amount, base_amount, is_discounted, paid_so_far = compute_checkout_price(user['id'], plan)
    order_code = generate_order_code()
    db = get_db()
    db.execute(
        '''INSERT INTO payment_orders
           (order_code, user_id, plan, amount, base_amount, is_discounted, method, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)''',
        (order_code, user['id'], plan, amount, base_amount, int(is_discounted), method, now_iso())
    )
    db.commit()

    if method == 'bank_transfer':
        return jsonify({
            'orderCode': order_code,
            'amount': amount,
            'baseAmount': base_amount,
            'isDiscounted': is_discounted,
            'method': 'bank_transfer',
            'qrImageUrl': vietqr_image_url(amount, order_code),
            'bankAccountName': VIETQR_ACCOUNT_NAME,
            'bankAccountNo': VIETQR_ACCOUNT_NO,
            'bankId': VIETQR_BANK_ID,
            'transferContent': order_code,
        })

    # method == 'vnpay'
    ip_addr = (request.headers.get('X-Forwarded-For', '') or request.remote_addr or '127.0.0.1').split(',')[0].strip()
    return_url = url_for('vnpay_return', _external=True)
    order_info = f"Nang cap {plan_meta(plan)['label']} 1 thang StudyMate AI - {order_code}"
    payment_url = vnpay_build_payment_url(order_code, amount, order_info, ip_addr, return_url)
    return jsonify({
        'orderCode': order_code, 'amount': amount, 'baseAmount': base_amount,
        'isDiscounted': is_discounted, 'method': 'vnpay', 'redirectUrl': payment_url,
    })


@app.route('/api/checkout/<order_code>/status', methods=['GET'])
@login_required
def api_checkout_status(order_code):
    """Client gọi định kỳ (polling) trong lúc chờ xác nhận chuyển khoản — hoặc để kiểm tra
    kết quả sau khi quay lại từ VNPAY. Chỉ trả về đơn của CHÍNH tài khoản đang đăng nhập."""
    db = get_db()
    order = db.execute(
        'SELECT * FROM payment_orders WHERE order_code = ? AND user_id = ?',
        (order_code, current_user_id())
    ).fetchone()
    if not order:
        return jsonify({"error": "Không tìm thấy đơn hàng."}), 404
    return jsonify({
        'orderCode': order['order_code'], 'plan': order['plan'], 'amount': order['amount'],
        'method': order['method'], 'status': order['status'],
    })


@app.route('/vnpay/return')
def vnpay_return():
    """VNPAY chuyển hướng trình duyệt của học sinh về đây sau khi thanh toán xong. Đây CHỈ
    là màn hình hiển thị kết quả cho người dùng xem — việc CHỐT đơn hàng (cộng gói) luôn dựa
    vào IPN (vnpay_ipn, server-to-server) bên dưới, vì Return URL có thể bị người dùng đóng
    trình duyệt giữa chừng hoặc giả mạo query string."""
    args = request.args.to_dict()
    valid = VNPAY_ENABLED and vnpay_verify_return(args)
    success = valid and args.get('vnp_ResponseCode') == '00'
    order_code = args.get('vnp_TxnRef', '')

    db = get_db()
    order = db.execute('SELECT * FROM payment_orders WHERE order_code = ?', (order_code,)).fetchone()
    status = order['status'] if order else None

    return render_template_string(
        VNPAY_RETURN_HTML,
        success=success, valid=valid, order_code=order_code, status=status,
        plan_label=plan_meta(order['plan'])['label'] if order else '',
    )


@app.route('/vnpay/ipn')
def vnpay_ipn():
    """IPN (Instant Payment Notification) — VNPAY tự gọi endpoint này từ SERVER của họ (không
    qua trình duyệt người dùng) để báo kết quả thanh toán CHÍNH THỨC. Đây là nơi DUY NHẤT được
    phép cộng gói cho tài khoản. Phải trả lời đúng định dạng JSON RspCode/Message VNPAY yêu cầu,
    nếu không VNPAY sẽ coi là thất bại và gọi lại nhiều lần."""
    args = request.args.to_dict()

    if not VNPAY_ENABLED or not vnpay_verify_return(args):
        return jsonify({"RspCode": "97", "Message": "Invalid signature"})

    order_code = args.get('vnp_TxnRef', '')
    db = get_db()
    order = db.execute('SELECT * FROM payment_orders WHERE order_code = ?', (order_code,)).fetchone()
    if not order:
        return jsonify({"RspCode": "01", "Message": "Order not found"})

    # Số tiền VNPAY gửi về đã nhân 100 — đối chiếu lại đúng số tiền đơn hàng gốc để tránh
    # trường hợp bị sửa amount trên đường truyền.
    try:
        vnp_amount = int(args.get('vnp_Amount', '0')) // 100
    except ValueError:
        vnp_amount = -1
    if vnp_amount != order['amount']:
        return jsonify({"RspCode": "04", "Message": "Invalid amount"})

    if order['status'] == 'paid':
        return jsonify({"RspCode": "02", "Message": "Order already confirmed"})

    if args.get('vnp_ResponseCode') == '00':
        db.execute(
            "UPDATE payment_orders SET status = 'paid', provider_txn_id = ?, paid_at = ? WHERE order_code = ?",
            (args.get('vnp_TransactionNo', ''), now_iso(), order_code)
        )
        db.commit()
        grant_plan_upgrade(order['user_id'], order['plan'], order_code, actor='vnpay_ipn')
        return jsonify({"RspCode": "00", "Message": "Confirm Success"})
    else:
        db.execute("UPDATE payment_orders SET status = 'failed' WHERE order_code = ?", (order_code,))
        db.commit()
        return jsonify({"RspCode": "00", "Message": "Confirm Success"})


# ==========================================
# 9. LỊCH SỬ HỘI THOẠI (theo tài khoản đăng nhập)
# ==========================================
@app.route('/api/conversations', methods=['GET'])
@login_required
def list_conversations():
    db = get_db()
    rows = db.execute(
        'SELECT id, title, updated_at, pinned, project_id FROM conversations '
        'WHERE user_id = ? ORDER BY pinned DESC, updated_at DESC',
        (current_user_id(),)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/conversations/<int:conv_id>/messages', methods=['GET'])
@login_required
def get_conversation_messages(conv_id):
    db = get_db()
    conv = db.execute(
        'SELECT id FROM conversations WHERE id = ? AND user_id = ?', (conv_id, current_user_id())
    ).fetchone()
    if not conv:
        return jsonify({"error": "Không tìm thấy đoạn chat này."}), 404

    rows = db.execute(
        'SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id ASC', (conv_id,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/conversations/<int:conv_id>', methods=['PATCH'])
@login_required
def update_conversation(conv_id):
    """Cập nhật 1 đoạn chat: đổi tên, ghim/bỏ ghim, hoặc chuyển vào một dự án."""
    db = get_db()
    conv = db.execute(
        'SELECT id FROM conversations WHERE id = ? AND user_id = ?', (conv_id, current_user_id())
    ).fetchone()
    if not conv:
        return jsonify({"error": "Không tìm thấy đoạn chat này."}), 404

    data = request.get_json(silent=True) or {}
    set_clauses, values = [], []

    if 'title' in data:
        title = (data.get('title') or '').strip()[:120]
        if title:
            set_clauses.append('title = ?')
            values.append(title)

    if 'pinned' in data:
        set_clauses.append('pinned = ?')
        values.append(1 if data.get('pinned') else 0)

    if 'project_id' in data:
        project_id = data.get('project_id')
        if project_id is not None:
            proj = db.execute(
                'SELECT id FROM projects WHERE id = ? AND user_id = ?', (project_id, current_user_id())
            ).fetchone()
            if not proj:
                return jsonify({"error": "Không tìm thấy dự án."}), 404
        set_clauses.append('project_id = ?')
        values.append(project_id)

    if not set_clauses:
        return jsonify({"error": "Không có nội dung nào để cập nhật."}), 400

    values.append(conv_id)
    db.execute(f'UPDATE conversations SET {", ".join(set_clauses)} WHERE id = ?', values)
    db.commit()
    return jsonify({"success": True})


@app.route('/api/conversations/<int:conv_id>', methods=['DELETE'])
@login_required
def delete_conversation(conv_id):
    db = get_db()
    conv = db.execute(
        'SELECT id FROM conversations WHERE id = ? AND user_id = ?', (conv_id, current_user_id())
    ).fetchone()
    if not conv:
        return jsonify({"error": "Không tìm thấy đoạn chat này."}), 404

    db.execute('DELETE FROM messages WHERE conversation_id = ?', (conv_id,))
    db.execute('DELETE FROM conversations WHERE id = ?', (conv_id,))
    db.commit()
    return jsonify({"success": True})


@app.route('/api/conversations/all', methods=['DELETE'])
@login_required
def delete_all_conversations():
    """Xoá toàn bộ lịch sử trò chuyện của tài khoản hiện tại (dùng trong Cài đặt)."""
    db = get_db()
    conv_ids = [r['id'] for r in db.execute(
        'SELECT id FROM conversations WHERE user_id = ?', (current_user_id(),)
    ).fetchall()]
    if conv_ids:
        placeholders = ','.join('?' * len(conv_ids))
        db.execute(f'DELETE FROM messages WHERE conversation_id IN ({placeholders})', conv_ids)
        db.execute('DELETE FROM conversations WHERE user_id = ?', (current_user_id(),))
        db.commit()
    return jsonify({"success": True, "deleted": len(conv_ids)})


# ==========================================
# 9.1 "DỰ ÁN" (giống Claude Projects) — nhóm các đoạn chat theo chủ đề
# ==========================================
@app.route('/api/projects', methods=['GET'])
@login_required
def list_projects():
    db = get_db()
    rows = db.execute(
        'SELECT id, name FROM projects WHERE user_id = ? ORDER BY name ASC', (current_user_id(),)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/projects', methods=['POST'])
@login_required
def create_project():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()[:60]
    if not name:
        return jsonify({"error": "Tên dự án không hợp lệ."}), 400
    db = get_db()
    cur = db.execute(
        'INSERT INTO projects (user_id, name, created_at) VALUES (?, ?, ?)',
        (current_user_id(), name, now_iso())
    )
    db.commit()
    return jsonify({"id": cur.lastrowid, "name": name})


@app.route('/api/projects/<int:proj_id>', methods=['DELETE'])
@login_required
def delete_project(proj_id):
    db = get_db()
    proj = db.execute(
        'SELECT id FROM projects WHERE id = ? AND user_id = ?', (proj_id, current_user_id())
    ).fetchone()
    if not proj:
        return jsonify({"error": "Không tìm thấy dự án."}), 404
    # Xoá dự án không xoá đoạn chat bên trong — chỉ gỡ nhóm, đoạn chat quay lại mục "Gần đây".
    db.execute('UPDATE conversations SET project_id = NULL WHERE project_id = ?', (proj_id,))
    db.execute('DELETE FROM projects WHERE id = ?', (proj_id,))
    db.commit()
    return jsonify({"success": True})


# ==========================================
# 9.2 TUỲ CHỈNH CÁ NHÂN (Cài đặt) + THÔNG BÁO HỆ THỐNG
# ==========================================
@app.route('/api/preferences', methods=['GET'])
@login_required
def get_preferences():
    return jsonify(get_user_preferences(current_user_id()))


@app.route('/api/preferences', methods=['POST'])
@login_required
def update_preferences():
    data = request.get_json(silent=True) or {}
    prefs = save_user_preferences(current_user_id(), data)
    return jsonify(prefs)


@app.route('/api/banner', methods=['GET'])
@login_required
def get_banner():
    return jsonify({
        "message": get_setting('banner_message', '') or '',
        "maintenance": get_setting('maintenance_mode', 'off') == 'on',
    })


# ==========================================
# 9.3 AI TUTOR TUỲ CHỈNH (Developer trở lên)
# ==========================================
@app.route('/api/tutors', methods=['GET'])
@developer_required
def list_tutors():
    db = get_db()
    rows = db.execute(
        'SELECT id, name, system_prompt, created_at FROM custom_tutors WHERE owner_id = ? ORDER BY id DESC',
        (current_user_id(),)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/tutors', methods=['POST'])
@developer_required
def create_tutor():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()[:60]
    system_prompt = (data.get('system_prompt') or '').strip()[:4000]
    if not name or not system_prompt:
        return jsonify({"error": "Cần nhập tên và nội dung hướng dẫn (system prompt) cho Tutor."}), 400
    db = get_db()
    cur = db.execute(
        'INSERT INTO custom_tutors (owner_id, name, system_prompt, created_at) VALUES (?, ?, ?, ?)',
        (current_user_id(), name, system_prompt, now_iso())
    )
    db.commit()
    return jsonify({"id": cur.lastrowid, "name": name, "system_prompt": system_prompt})


@app.route('/api/tutors/<int:tutor_id>', methods=['DELETE'])
@developer_required
def delete_tutor(tutor_id):
    db = get_db()
    tutor = db.execute(
        'SELECT id FROM custom_tutors WHERE id = ? AND owner_id = ?', (tutor_id, current_user_id())
    ).fetchone()
    if not tutor:
        return jsonify({"error": "Không tìm thấy AI Tutor này."}), 404
    db.execute('DELETE FROM custom_tutors WHERE id = ?', (tutor_id,))
    db.commit()
    return jsonify({"success": True})


# ==========================================
# 9.4 API KEY (Developer trở lên) — quản lý key + endpoint xác thực demo
# ==========================================
def _hash_api_key(raw_key):
    import hashlib
    return hashlib.sha256(raw_key.encode('utf-8')).hexdigest()


@app.route('/api/keys', methods=['GET'])
@developer_required
def list_api_keys():
    db = get_db()
    rows = db.execute(
        'SELECT id, name, key_prefix, created_at, last_used_at, revoked FROM api_keys '
        'WHERE user_id = ? ORDER BY id DESC',
        (current_user_id(),)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/keys', methods=['POST'])
@developer_required
def create_api_key():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or 'Key không tên').strip()[:60]
    raw_key = 'sm_' + secrets.token_urlsafe(32)
    key_hash = _hash_api_key(raw_key)
    key_prefix = raw_key[:12] + '…'
    db = get_db()
    cur = db.execute(
        'INSERT INTO api_keys (user_id, name, key_prefix, key_hash, created_at) VALUES (?, ?, ?, ?, ?)',
        (current_user_id(), name, key_prefix, key_hash, now_iso())
    )
    db.commit()
    # Key gốc CHỈ hiển thị đúng 1 lần lúc tạo — sau đó server chỉ còn giữ bản băm (hash).
    return jsonify({"id": cur.lastrowid, "name": name, "key": raw_key, "key_prefix": key_prefix})


@app.route('/api/keys/<int:key_id>', methods=['DELETE'])
@developer_required
def revoke_api_key(key_id):
    db = get_db()
    key_row = db.execute(
        'SELECT id FROM api_keys WHERE id = ? AND user_id = ?', (key_id, current_user_id())
    ).fetchone()
    if not key_row:
        return jsonify({"error": "Không tìm thấy API Key này."}), 404
    db.execute('UPDATE api_keys SET revoked = 1 WHERE id = ?', (key_id,))
    db.commit()
    return jsonify({"success": True})


@app.route('/api/v1/ping', methods=['GET'])
def api_v1_ping():
    """Endpoint demo để xác nhận cơ chế xác thực bằng API Key hoạt động thật (không phải giả lập).
    Header: Authorization: Bearer <api_key>. Đây là điểm khởi đầu hạ tầng — chưa có endpoint
    /api/v1/chat đầy đủ (xem ghi chú 'Chưa làm' trong README)."""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({"error": "Thiếu API Key (header Authorization: Bearer <key>)."}), 401
    raw_key = auth_header[len('Bearer '):].strip()
    key_hash = _hash_api_key(raw_key)
    db = get_db()
    row = db.execute(
        'SELECT ak.id, u.username FROM api_keys ak JOIN users u ON u.id = ak.user_id '
        'WHERE ak.key_hash = ? AND ak.revoked = 0',
        (key_hash,)
    ).fetchone()
    if not row:
        return jsonify({"error": "API Key không hợp lệ hoặc đã bị thu hồi."}), 401
    db.execute('UPDATE api_keys SET last_used_at = ? WHERE id = ?', (now_iso(), row['id']))
    db.commit()
    return jsonify({"ok": True, "user": row['username'], "message": "API Key hợp lệ."})


# ==========================================
# 9.5 PLAYGROUND (Developer trở lên) — thử prompt trực tiếp, không lưu vào lịch sử chat
# ==========================================
@app.route('/api/playground', methods=['POST'])
@developer_required
def playground_run():
    data = request.get_json(silent=True) or {}
    system_prompt = (data.get('system_prompt') or 'Bạn là một trợ lý AI hữu ích.').strip()[:4000]
    user_message = (data.get('message') or '').strip()[:4000]
    if not user_message:
        return jsonify({"error": "Nhập nội dung để thử nghiệm."}), 400

    def generate():
        try:
            for token in stream_consolex_ai(system_prompt, user_message):
                yield f"data: {json.dumps({'token': token})}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'Connection': 'keep-alive'},
    )


# ==========================================
# 10. CHAT (STREAMING QUA SERVER-SENT EVENTS) + LƯU LỊCH SỬ
# ==========================================
@app.route('/api/chat', methods=['POST'])
@login_required
def chat():
    data = request.get_json(silent=True) or {}
    subject = data.get('subject', 'Toán Học')
    mode = data.get('mode', 'Giải thích')
    user_message = (data.get('message') or '').strip()
    file_context = (data.get('fileContext') or '').strip()
    file_name = (data.get('fileName') or '').strip()
    image_data = (data.get('imageData') or '').strip()
    raw_conv_id = data.get('conversationId')
    raw_tutor_id = data.get('tutorId')
    raw_thinking_mode = (data.get('thinkingMode') or 'standard').strip()

    role = current_user_role()
    unlimited = role_rank(role) >= role_rank('admin')  # Admin/Super Admin: không giới hạn độ dài tin nhắn

    user_for_plan = current_user()
    plan = effective_plan(user_for_plan)
    # Chốt lại chế độ suy nghĩ hợp lệ theo gói — nếu client cố gửi thẳng 1 chế độ đang bị khoá
    # (vd sửa tay request API), tự động rơi về "Trợ Lý" (standard) thay vì tin tưởng client.
    thinking_mode = resolve_thinking_mode(raw_thinking_mode, plan)
    tm_conf = THINKING_MODES[thinking_mode]
    app_name = app_display_name(user_for_plan)

    # Chế độ bảo trì: chặn học sinh thường, Admin trở lên vẫn dùng được để kiểm tra hệ thống.
    if get_setting('maintenance_mode', 'off') == 'on' and role_rank(role) < role_rank('admin'):
        return jsonify({"error": "Hệ thống đang bảo trì, em quay lại sau ít phút nhé! 🛠️"}), 503

    if not user_message:
        return jsonify({"error": "Em chưa nhập câu hỏi nào cả."}), 400

    # Input validation cơ bản để tránh payload bất thường.
    if not unlimited and len(user_message) > 4000:
        return jsonify({"error": "Câu hỏi quá dài, em rút gọn lại giúp Thầy/Cô nhé!"}), 400
    if image_data and not image_data.startswith('data:image/'):
        return jsonify({"error": "Dữ liệu ảnh không hợp lệ."}), 400

    user_id = current_user_id()
    db = get_db()

    # AI Tutor tuỳ chỉnh (Developer trở lên): nếu chọn 1 tutor riêng, dùng system prompt của
    # tutor đó thay cho prompt mặc định theo Môn học/Chế độ.
    custom_tutor = None
    if raw_tutor_id and role_rank(role) >= role_rank('developer'):
        try:
            tutor_id = int(raw_tutor_id)
        except (TypeError, ValueError):
            tutor_id = None
        if tutor_id is not None:
            custom_tutor = db.execute(
                'SELECT id, name, system_prompt FROM custom_tutors WHERE id = ? AND owner_id = ?',
                (tutor_id, user_id)
            ).fetchone()

    # Xác định (hoặc tạo mới) đoạn hội thoại để lưu lịch sử theo tài khoản.
    conv_id = None
    if raw_conv_id is not None:
        try:
            candidate_id = int(raw_conv_id)
        except (TypeError, ValueError):
            candidate_id = None
        if candidate_id is not None:
            existing = db.execute(
                'SELECT id FROM conversations WHERE id = ? AND user_id = ?', (candidate_id, user_id)
            ).fetchone()
            if existing:
                conv_id = existing['id']

    if conv_id is None:
        title = user_message if len(user_message) <= 40 else (user_message[:40] + '…')
        cur = db.execute(
            'INSERT INTO conversations (user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)',
            (user_id, title or 'Đoạn chat mới', now_iso(), now_iso())
        )
        db.commit()
        conv_id = cur.lastrowid

    db.execute(
        'INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)',
        (conv_id, 'user', user_message, now_iso())
    )
    db.commit()

    # "Bộ nhớ" AI: phát hiện + lưu 1 mục mới từ tin nhắn này (nếu có), rồi lấy những gì đã
    # ghi nhớ trước đó để cá nhân hoá câu trả lời (xem mục 0.27).
    new_memory = extract_and_save_memory(user_id, user_message)
    recent_memories = get_recent_memories(user_id)

    if custom_tutor:
        system_prompt = f"""
    Bạn là "{custom_tutor['name']}", một AI Tutor tuỳ chỉnh do chính người dùng tạo ra trên {app_name}.
    Hãy làm theo đúng hướng dẫn/vai trò sau đây do người tạo đặt ra:
    ---
    {custom_tutor['system_prompt']}
    ---
    Vẫn dùng Markdown để trình bày rõ ràng, dễ đọc.
    Với công thức/phép toán: đặt trong cú pháp LaTeX chuẩn ("$$...$$" cho dòng riêng, "\\(...\\)" cho công thức ngắn giữa câu).
    """
    else:
        system_prompt = f"""
    Bạn là {app_name}, một gia sư AI tận tâm cho học sinh.
    Môn học: {subject}. Chế độ: {mode}.
    Quy tắc:
    1. Xưng "Thầy/Cô", gọi "em".
    2. Dùng Markdown, Emoji, giải thích dễ hiểu, không quá học thuật.
    3. Tuân thủ chế độ:
       - Giải thích: Giải thích bản chất.
       - Gợi ý: Chỉ gợi ý bước làm, KHÔNG giải hộ.
       - Kiểm tra: Sửa lỗi, khen ngợi.
       - Luyện tập: Cho 1-2 bài tập.
       - Ôn tập: Tóm tắt trọng tâm.
    4. Với công thức/phép toán: LUÔN đặt trong cú pháp LaTeX chuẩn — công thức trên
       dòng riêng thì bọc trong "$$...$$", công thức ngắn giữa câu thì bọc trong
       "\\(...\\)". Không viết công thức dưới dạng chữ thường lẫn trong đoạn văn.
    """

    # "Chế độ suy nghĩ" (Trợ Lý/Học Giả/Giáo Sư/Thiên Tài) — Học Giả/Giáo Sư mở khoá từ gói
    # Premium, Thiên Tài độc quyền gói Max. Chỉ thêm hướng dẫn khi khác "Trợ Lý" mặc định.
    if tm_conf['prompt_hint']:
        system_prompt += f"""

    Chế độ suy nghĩ đang bật: "{tm_conf['icon']} {tm_conf['label']}". {tm_conf['prompt_hint']}
    """

    # Admin có thể thêm 1 đoạn hướng dẫn chung áp dụng cho MỌI cuộc trò chuyện (vd: quy định
    # riêng của trường/lớp) từ trang /developer, không cần sửa code.
    global_addendum = get_setting('global_system_addendum', '')
    if global_addendum:
        system_prompt += f"""

    Hướng dẫn bổ sung từ quản trị viên hệ thống (áp dụng cho mọi cuộc trò chuyện):
    ---
    {global_addendum}
    ---
    """

    if recent_memories:
        mem_lines = "\n".join(f"    - {m}" for m in recent_memories)
        system_prompt += f"""

    Những điều Thầy/Cô đã ghi nhớ về học sinh này từ các lần trò chuyện trước:
{mem_lines}
    Hãy tận dụng thông tin này để cá nhân hoá câu trả lời khi phù hợp (vd: nếu biết học sinh
    hay nhầm 1 lỗi cụ thể, hãy giải thích kỹ hơn ở phần đó), nhưng đừng nhắc lại y nguyên nếu
    không cần thiết.
    """

    if file_context:
        system_prompt += f"""

    Học sinh đã tải lên file "{file_name}" với nội dung trích xuất như sau (có thể không đầy đủ):
    ---
    {file_context}
    ---
    Hãy dùng nội dung này để trả lời câu hỏi của học sinh khi liên quan.
    """

    if image_data:
        system_prompt += """

    Học sinh đã đính kèm một hình ảnh (ví dụ: đề bài chụp, bài làm viết tay, biểu đồ...).
    Hãy quan sát kỹ nội dung trong ảnh để trả lời câu hỏi của học sinh.
    """

    if image_data:
        user_content = [
            {"type": "text", "text": user_message},
            {"type": "image_url", "image_url": {"url": image_data}},
        ]
    else:
        user_content = user_message

    def generate():
        yield f"data: {json.dumps({'conversationId': conv_id, 'thinkingMode': thinking_mode})}\n\n"
        if new_memory:
            yield f"data: {json.dumps({'memory': new_memory})}\n\n"
        collected = []
        try:
            for token in stream_consolex_ai(system_prompt, user_content, max_tokens=tm_conf['max_tokens']):
                collected.append(token)
                yield f"data: {json.dumps({'token': token})}\n\n"

            assistant_text = ''.join(collected).strip()
            if assistant_text:
                # Kết nối riêng (không dùng `db`/`g` của request) — xem giải thích chi tiết
                # ở docstring của open_write_db(): tới lúc này request context gốc có thể
                # đã bị teardown (đóng kết nối `db`) trước khi generator chạy tới đây.
                write_conn = open_write_db()
                try:
                    write_conn.execute(
                        'INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)',
                        (conv_id, 'assistant', assistant_text, now_iso())
                    )
                    write_conn.execute('UPDATE conversations SET updated_at = ? WHERE id = ?', (now_iso(), conv_id))
                    write_conn.commit()
                finally:
                    write_conn.close()

            log_usage(user_id, subject, mode, len(user_message), len(assistant_text),
                      bool(file_context), bool(image_data), 'ok' if assistant_text else 'empty')

            if assistant_text:
                track_topic_practice(user_id, subject, mode)
                gamify = award_xp_and_streak(user_id)
                yield f"data: {json.dumps({'gamify': gamify})}\n\n"

            yield f"data: {json.dumps({'done': True})}\n\n"
        except RuntimeError as e:
            log_usage(user_id, subject, mode, len(user_message), 0, bool(file_context), bool(image_data), 'error')
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        except requests.exceptions.RequestException as e:
            log_usage(user_id, subject, mode, len(user_message), 0, bool(file_context), bool(image_data), 'error')
            yield f"data: {json.dumps({'error': f'Lỗi kết nối tới xAI: {e}'})}\n\n"
        except (KeyError, IndexError, ValueError) as e:
            log_usage(user_id, subject, mode, len(user_message), 0, bool(file_context), bool(image_data), 'error')
            yield f"data: {json.dumps({'error': f'Phản hồi từ xAI không đúng định dạng: {e}'})}\n\n"
        except Exception as e:
            log_usage(user_id, subject, mode, len(user_message), 0, bool(file_context), bool(image_data), 'error')
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',  # tắt buffer ở Nginx nếu có, để stream tới ngay
            'Connection': 'keep-alive',
        },
    )

# Cuối file, TRƯỚC if __name__ == '__main__':
# hoặc ngay sau khi define xong init_db + app routes cũng được,
# miễn là chạy 1 lần lúc load module:
try:
    init_db()
except Exception as e:
    print(f"⚠️ init_db failed: {e}")
    raise

if __name__ == '__main__':
    init_db()
    print("🚀 StudyMate AI đang chạy... Truy cập: http://localhost:5000")
    print("👤 Trang đăng nhập: http://localhost:5000/login")
    print(f"🔑 Đăng nhập Google: {'BẬT' if GOOGLE_OAUTH_ENABLED else 'tắt (chưa cấu hình .env)'}")
    print("🛡️ Để xem bảng báo cáo bảo mật... Truy cập: http://localhost:5000/security")
    # debug=True chỉ dùng khi phát triển trên máy cá nhân — KHÔNG bật khi deploy thật
    # (xem README phần "Deploy lên production" để chạy bằng gunicorn thay vì app.run).
    # use_reloader=False: khi debug=True, Werkzeug mặc định tự khởi động lại
    # (restart) tiến trình mỗi khi phát hiện một file trong thư mục dự án thay
    # đổi. studymate.db (SQLite) bị ghi liên tục mỗi khi có tin nhắn mới, nên
    # nó cũng bị coi là "file thay đổi" và làm server tự restart ngay giữa lúc
    # đang stream câu trả lời — kết nối SQLite của request đó bị đóng đột ngột,
    # gây lỗi "Cannot operate on a closed database.". Tắt use_reloader để tránh
    # restart ngoài ý muốn này (vẫn giữ debug=True để còn thấy traceback lỗi khi
    # phát triển). Khi sửa code .py, chỉ cần dừng (Ctrl+C) và chạy lại thủ công.
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True, use_reloader=False)

import os
import io
import re
import json
import base64
import secrets
import sqlite3
import requests
import importlib
from datetime import datetime, timezone, timedelta
from functools import wraps
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
app.config['MAX_CONTENT_LENGTH'] = 15 * 1024 * 1024  # giới hạn upload 15MB
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

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

# Giới hạn số ký tự trích từ file để tránh vượt quá token limit khi gửi cho AI
MAX_FILE_CHARS = 12000
MAX_IMAGE_BYTES = 6 * 1024 * 1024
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
    conn.commit()

    conn.execute('''
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    conn.commit()

    # Báo lỗi từ học sinh — mỗi báo cáo gắn với 1 câu trả lời cụ thể (nếu có) để developer xem lại.
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
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    # "Bộ nhớ" AI — những điều đáng nhớ về 1 học sinh (học sinh chủ động nhờ ghi nhớ, hoặc
    # hệ thống tự nhận diện vài tín hiệu đơn giản như lớp học). Dùng để cá nhân hoá câu trả
    # lời ở các lượt chat sau, và cũng được tổng hợp lại cho developer xem ở trang thống kê.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'auto',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.commit()

    # ---- Di trú (migration) cho database cũ đã tồn tại trước khi có cột "role" ----
    existing_cols = [r[1] for r in conn.execute('PRAGMA table_info(users)').fetchall()]
    if 'role' not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
        conn.commit()
    if 'preferences' not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN preferences TEXT")
        conn.commit()

    conv_cols = [r[1] for r in conn.execute('PRAGMA table_info(conversations)').fetchall()]
    if 'pinned' not in conv_cols:
        conn.execute("ALTER TABLE conversations ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    if 'project_id' not in conv_cols:
        conn.execute("ALTER TABLE conversations ADD COLUMN project_id INTEGER")
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
# 0.2. XÁC THỰC NGƯỜI DÙNG (session-based)
# ==========================================
def current_user_id():
    return session.get('user_id')


def current_user_role():
    return session.get('role', 'user')


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user_id():
            if request.path.startswith('/api/'):
                return jsonify({"error": "Vui lòng đăng nhập để tiếp tục."}), 401
            return redirect(url_for('login_page', next=request.path))
        return view(*args, **kwargs)
    return wrapped


def developer_required(view):
    """Chỉ cho phép tài khoản có role = 'developer' truy cập (vd: trang thống kê)."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        wants_json = request.path.startswith('/api/') or request.method != 'GET'
        if not current_user_id():
            if wants_json:
                return jsonify({"error": "Vui lòng đăng nhập để tiếp tục."}), 401
            return redirect(url_for('login_page', next=request.path))
        if current_user_role() != 'developer':
            if wants_json:
                return jsonify({"error": "Bạn không có quyền truy cập chức năng này."}), 403
            flash('Tài khoản của em không có quyền truy cập trang này.')
            return redirect(url_for('home'))
        return view(*args, **kwargs)
    return wrapped


def log_usage(user_id, subject, mode, message_chars, response_chars, had_file, had_image, status):
    """Ghi lại 1 lượt sử dụng AI vào bảng usage_logs, phục vụ trang thống kê developer."""
    try:
        db = get_db()
        db.execute(
            '''INSERT INTO usage_logs
               (user_id, endpoint, subject, mode, message_chars, response_chars, had_file, had_image, status, created_at)
               VALUES (?, 'chat', ?, ?, ?, ?, ?, ?, ?, ?)''',
            (user_id, subject, mode, message_chars, response_chars, int(bool(had_file)), int(bool(had_image)),
             status, now_iso())
        )
        db.commit()
    except Exception:
        # Không để lỗi ghi log ảnh hưởng tới trải nghiệm chat của học sinh.
        pass


# ==========================================
# 0.3. TÙY CHỌN NGƯỜI DÙNG (preferences), DỰ ÁN (projects), CÀI ĐẶT HỆ THỐNG
# ==========================================
DEFAULT_PREFERENCES = {
    "theme": "system",       # light | dark | system
    "language": "vi",        # vi | en
    "default_subject": "",
    "default_mode": "",
}


def get_preferences(user_id):
    db = get_db()
    row = db.execute('SELECT preferences FROM users WHERE id = ?', (user_id,)).fetchone()
    prefs = dict(DEFAULT_PREFERENCES)
    if row and row['preferences']:
        try:
            prefs.update(json.loads(row['preferences']))
        except Exception:
            pass
    return prefs


def set_preferences(user_id, updates):
    prefs = get_preferences(user_id)
    for k in DEFAULT_PREFERENCES:
        if k in updates:
            prefs[k] = updates[k]
    db = get_db()
    db.execute('UPDATE users SET preferences = ? WHERE id = ?', (json.dumps(prefs), user_id))
    db.commit()
    return prefs


def get_setting(key, default=None):
    db = get_db()
    row = db.execute('SELECT value FROM system_settings WHERE key = ?', (key,)).fetchone()
    if row is None:
        return default
    return row['value']


def set_setting(key, value):
    db = get_db()
    db.execute(
        'INSERT INTO system_settings (key, value) VALUES (?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
        (key, value)
    )
    db.commit()


def google_login_effective():
    """Cờ bật/tắt đăng nhập Google tại runtime, do developer điều khiển, độc lập với .env.
    Giá trị trong system_settings: 'on' / 'off' / (không đặt = theo cấu hình .env mặc định)."""
    if not GOOGLE_OAUTH_ENABLED:
        return False
    override = get_setting('google_login_override')
    if override == 'off':
        return False
    if override == 'on':
        return True
    return True  # mặc định: bật nếu .env đã cấu hình client id/secret


def get_banner():
    return {
        "text": get_setting('banner_text', '') or '',
        "active": get_setting('banner_active', '0') == '1',
    }


# ==========================================
# 0.4. "BỘ NHỚ" AI (memories) — cá nhân hoá + báo lỗi (issue reports)
# ==========================================
# Học sinh chủ động nhờ AI ghi nhớ điều gì đó, ví dụ: "ghi nhớ giúp em là em học lớp 8"
# hoặc "hãy nhớ mình sắp thi học kỳ môn Hóa". Không cần gọi thêm API AI nào — chỉ dùng
# regex đơn giản, chạy nhanh và không tốn chi phí.
MEMORY_TRIGGER_RE = re.compile(
    r'(?:ghi\s*nhớ|hãy\s*nhớ|nhớ\s*giúp|note\s*giúp|lưu\s*ý\s*giúp)(?:\s*(?:em|mình|giúp|rằng|là))*\s*[:,-]?\s*(.+)',
    re.IGNORECASE
)
GRADE_LEVEL_RE = re.compile(r'\blớp\s*(6|7|8|9)\b', re.IGNORECASE)
MAX_MEMORY_LEN = 300
MAX_MEMORIES_IN_PROMPT = 5


def save_memory(user_id, content, source='auto'):
    """Lưu 1 mục bộ nhớ cho học sinh. Không để lỗi ở đây ảnh hưởng tới luồng chat chính."""
    content = (content or '').strip()
    if not content:
        return
    content = content[:MAX_MEMORY_LEN]
    try:
        db = get_db()
        db.execute(
            'INSERT INTO memories (user_id, content, source, created_at) VALUES (?, ?, ?, ?)',
            (user_id, content, source, now_iso())
        )
        db.commit()
    except Exception:
        pass


def extract_and_save_memory(user_id, user_message):
    """Phát hiện + lưu 1 'bộ nhớ' mới từ tin nhắn của học sinh (nếu có).
    Trả về nội dung vừa ghi nhớ (để báo lại cho học sinh biết), hoặc None nếu không có gì."""
    text = (user_message or '').strip()
    if not text:
        return None

    # 1) Học sinh chủ động yêu cầu ghi nhớ — ưu tiên cao nhất.
    m = MEMORY_TRIGGER_RE.search(text)
    if m:
        content = m.group(1).strip(' .!?')
        if content:
            save_memory(user_id, content, source='explicit')
            return content

    # 2) Tự nhận diện lớp học (chỉ lưu 1 lần, tránh lặp lại mỗi khi học sinh gõ "lớp 8").
    g = GRADE_LEVEL_RE.search(text)
    if g:
        try:
            db = get_db()
            existing = db.execute(
                "SELECT id FROM memories WHERE user_id = ? AND content LIKE 'Học sinh đang học lớp%'",
                (user_id,)
            ).fetchone()
            if not existing:
                content = f"Học sinh đang học lớp {g.group(1)}."
                save_memory(user_id, content, source='auto')
                return content
        except Exception:
            pass

    return None


def get_recent_memories(user_id, limit=MAX_MEMORIES_IN_PROMPT):
    db = get_db()
    rows = db.execute(
        'SELECT content FROM memories WHERE user_id = ? ORDER BY created_at DESC LIMIT ?',
        (user_id, limit)
    ).fetchall()
    return [r['content'] for r in reversed(rows)]  # cũ -> mới, đọc tự nhiên hơn trong prompt


# ==========================================
# 1. GIAO DIỆN ĐĂNG NHẬP / ĐĂNG KÝ
# ==========================================
AUTH_HTML = r'''
<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{ 'Đăng nhập' if mode == 'login' else 'Đăng ký' }} — StudyMate AI Pro</title>
  <script src="https://cdn.tailwindcss.com"></script>
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
      <h1 class="text-2xl font-extrabold mt-4 text-white tracking-tight">StudyMate AI Pro</h1>
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

    <p class="text-center text-xs text-indigo-200/60 mt-6">© {{ 2026 }} StudyMate AI Pro — Dữ liệu đăng nhập được mã hoá, không chia sẻ cho bên thứ ba.</p>
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
  <title>StudyMate AI Pro - Gia sư THCS</title>
  <script src="https://cdn.tailwindcss.com"></script>
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
    .msg-actions.force-visible, .ai-msg-group:hover .msg-actions { opacity: 1; }

    @keyframes memoryToastFade {
      0% { opacity: 0; transform: translate(-50%, 6px); }
      10%, 85% { opacity: 1; transform: translate(-50%, 0); }
      100% { opacity: 0; transform: translate(-50%, 6px); }
    }
    .memory-toast { animation: memoryToastFade 3.5s ease forwards; }

    /* ---- Modal system ---- */
    #modalOverlay.open { display: flex; }
    .modal-card.open { display: block; }
    .conv-item { position: relative; }
    .conv-menu-btn { opacity: 0; }
    .conv-item:hover .conv-menu-btn, .conv-menu-btn.force-visible { opacity: 1; }
    .conv-menu-dropdown {
      position: absolute; right: 0; top: 100%; margin-top: 4px; z-index: 30;
      min-width: 160px; background: white; border: 1px solid #e5e7eb; border-radius: 0.75rem;
      box-shadow: 0 10px 25px rgba(0,0,0,0.12); overflow: hidden;
    }
    .dark .conv-menu-dropdown { background: #1f2937; border-color: #374151; }
    .conv-menu-dropdown button {
      width: 100%; text-align: left; padding: 0.5rem 0.9rem; font-size: 0.8rem;
      display: flex; align-items: center; gap: 0.5rem;
    }
    .conv-menu-dropdown button:hover { background: rgba(0,0,0,0.05); }
    .dark .conv-menu-dropdown button:hover { background: rgba(255,255,255,0.08); }

    #sidebar { transition: transform 0.2s ease; }
    @media (max-width: 1023px) { #sidebar { transform: translateX(-100%); } #sidebar.open { transform: translateX(0); } }

    .conv-item .del-btn { opacity: 0; }
    .conv-item:hover .del-btn { opacity: 1; }

    textarea#messageInput { max-height: 160px; }
  </style>
</head>
<body class="h-screen overflow-hidden bg-white dark:bg-[#212121] text-gray-800 dark:text-gray-100">

<div class="flex h-screen">

  <!-- ===================== SIDEBAR ===================== -->
  <aside id="sidebar" class="fixed lg:static inset-y-0 left-0 z-50 w-72 flex-shrink-0 bg-gray-50 dark:bg-[#171717] border-r border-gray-200 dark:border-gray-800 flex flex-col">
    <div class="p-3 flex items-center justify-between">
      <div class="flex items-center gap-2 px-1">
        <div class="w-8 h-8 rounded-lg bg-gradient-to-br from-blue-600 to-indigo-600 flex items-center justify-center text-white font-bold text-sm">S</div>
        <span class="font-bold text-base">StudyMate AI</span>
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
        <input id="convSearchInput" type="text" data-i18n-placeholder="search_chats" placeholder="Tìm đoạn chat..."
               class="w-full pl-8 pr-3 py-2 text-sm rounded-xl bg-gray-100 dark:bg-gray-800 border-0 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:text-white">
      </div>
    </div>

    <div class="px-3 mt-4 mb-1 flex items-center justify-between">
      <span class="text-xs font-semibold text-gray-400 uppercase tracking-wide" data-i18n="projects">Dự án</span>
      <button id="newProjectBtn" class="w-5 h-5 flex items-center justify-center rounded hover:bg-gray-200 dark:hover:bg-gray-800 text-gray-400" title="Tạo dự án mới">
        <i class="fas fa-plus text-xs"></i>
      </button>
    </div>
    <div id="projectList" class="px-3 space-y-0.5 text-sm"></div>

    <div id="pinnedSection" class="hidden">
      <div class="px-3 mt-4 mb-1 text-xs font-semibold text-gray-400 uppercase tracking-wide" data-i18n="pinned">Đã ghim</div>
      <div id="pinnedList" class="px-3 space-y-0.5 text-sm"></div>
    </div>

    <div class="px-3 mt-4 mb-1 text-xs font-semibold text-gray-400 uppercase tracking-wide" data-i18n="recent">Gần đây</div>
    <div id="convList" class="flex-1 overflow-y-auto px-3 pb-3 space-y-0.5 text-sm"></div>

    <div id="systemBanner" class="hidden mx-3 mb-2 px-3 py-2 rounded-lg bg-indigo-50 dark:bg-indigo-900/30 border border-indigo-200 dark:border-indigo-800 text-xs text-indigo-700 dark:text-indigo-300 flex items-start gap-2">
      <i class="fas fa-bullhorn mt-0.5"></i>
      <span id="systemBannerText" class="flex-1"></span>
      <button id="systemBannerClose" class="text-indigo-400 hover:text-indigo-600 flex-shrink-0"><i class="fas fa-xmark"></i></button>
    </div>

    <div class="border-t border-gray-200 dark:border-gray-800 p-3 relative">
      <button id="userMenuBtn" type="button" class="w-full flex items-center gap-3 px-2 py-2 rounded-xl hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors">
        <div id="userAvatar" class="w-8 h-8 rounded-full bg-indigo-600 text-white flex items-center justify-center font-bold text-sm flex-shrink-0"></div>
        <span id="userNameLabel" class="flex-1 text-left truncate font-medium text-sm"></span>
        <i class="fas fa-chevron-up text-xs text-gray-400"></i>
      </button>
      <div id="userMenu" class="hidden absolute bottom-[64px] left-3 right-3 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl shadow-lg overflow-hidden z-10">
        <button id="openSettingsBtn" type="button" class="w-full flex items-center gap-2 px-4 py-3 text-sm hover:bg-gray-100 dark:hover:bg-gray-700 text-left">
          <i class="fas fa-gear text-gray-400 w-4"></i> <span data-i18n="settings">Cài đặt</span>
        </button>
        <button id="openHelpBtn" type="button" class="w-full flex items-center gap-2 px-4 py-3 text-sm hover:bg-gray-100 dark:hover:bg-gray-700 text-left">
          <i class="fas fa-circle-question text-gray-400 w-4"></i> <span data-i18n="help">Trợ giúp &amp; phím tắt</span>
        </button>
        <button id="openUpgradeBtn" type="button" class="w-full flex items-center gap-2 px-4 py-3 text-sm hover:bg-gray-100 dark:hover:bg-gray-700 text-left border-b border-gray-100 dark:border-gray-700">
          <i class="fas fa-arrow-up-right-dots text-gray-400 w-4"></i> <span data-i18n="upgrade">Nâng cấp gói</span>
        </button>
        {% if is_developer %}
        <a href="/developer" class="flex items-center gap-2 px-4 py-3 text-sm hover:bg-gray-100 dark:hover:bg-gray-700 text-indigo-600 dark:text-indigo-400 border-b border-gray-100 dark:border-gray-700">
          <i class="fas fa-chart-line w-4"></i> Thống kê (Developer)
        </a>
        {% endif %}
        <a href="/logout" class="flex items-center gap-2 px-4 py-3 text-sm hover:bg-gray-100 dark:hover:bg-gray-700 text-red-600 dark:text-red-400">
          <i class="fas fa-right-from-bracket w-4"></i> <span data-i18n="logout">Đăng xuất</span>
        </a>
      </div>
    </div>
  </aside>
  <div id="sidebarOverlay" class="hidden fixed inset-0 bg-black/40 z-40 lg:hidden"></div>

  <!-- ===================== MODALS ===================== -->
  <div id="modalOverlay" class="hidden fixed inset-0 bg-black/50 z-[60] flex items-center justify-center p-4">

    <div id="settingsModal" class="modal-card hidden bg-white dark:bg-gray-900 rounded-2xl shadow-2xl w-full max-w-md max-h-[85vh] overflow-y-auto">
      <div class="flex items-center justify-between px-5 py-4 border-b border-gray-200 dark:border-gray-800">
        <h3 class="font-semibold text-lg" data-i18n="settings">Cài đặt</h3>
        <button class="modal-close-btn text-gray-400 hover:text-gray-600 w-8 h-8 flex items-center justify-center"><i class="fas fa-xmark"></i></button>
      </div>
      <div class="p-5 space-y-5 text-sm">
        <div>
          <label class="block font-medium mb-1.5" data-i18n="theme">Giao diện</label>
          <select id="settingTheme" class="w-full px-3 py-2 rounded-lg bg-gray-100 dark:bg-gray-800 border-0 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:text-white">
            <option value="light" data-i18n="theme_light">Sáng</option>
            <option value="dark" data-i18n="theme_dark">Tối</option>
            <option value="system" data-i18n="theme_system">Theo hệ thống</option>
          </select>
        </div>
        <div>
          <label class="block font-medium mb-1.5" data-i18n="language">Ngôn ngữ</label>
          <select id="settingLanguage" class="w-full px-3 py-2 rounded-lg bg-gray-100 dark:bg-gray-800 border-0 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:text-white">
            <option value="vi">Tiếng Việt</option>
            <option value="en">English</option>
          </select>
        </div>
        <div>
          <label class="block font-medium mb-1.5" data-i18n="default_subject">Môn học mặc định</label>
          <select id="settingDefaultSubject" class="w-full px-3 py-2 rounded-lg bg-gray-100 dark:bg-gray-800 border-0 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:text-white">
            <option value="">— <span data-i18n="none">Không</span> —</option>
            <option value="Toán">📐 Toán Học</option>
            <option value="Ngữ Văn">📖 Ngữ Văn</option>
            <option value="Tiếng Anh">🇬🇧 Tiếng Anh</option>
            <option value="Vật Lý">⚛️ Vật Lý</option>
            <option value="Hóa Học">🧪 Hóa Học</option>
            <option value="Sinh Học">🌱 Sinh Học</option>
            <option value="Lịch sử & Địa lý">🌍 Lịch sử & Địa lý</option>
            <option value="Tin Học">💻 Tin Học</option>
          </select>
        </div>
        <div>
          <label class="block font-medium mb-1.5" data-i18n="default_mode">Chế độ mặc định</label>
          <select id="settingDefaultMode" class="w-full px-3 py-2 rounded-lg bg-gray-100 dark:bg-gray-800 border-0 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:text-white">
            <option value="">— <span data-i18n="none">Không</span> —</option>
            <option value="Giải thích">📘 Giải Thích Dễ Hiểu</option>
            <option value="Gợi ý">💡 Gợi Ý Từng Bước</option>
            <option value="Kiểm tra bài làm">✅ Kiểm Tra Bài Làm</option>
            <option value="Luyện tập">📝 Ra Bài Luyện Tập</option>
            <option value="Ôn tập">🔄 Tổng Hợp Ôn Tập</option>
          </select>
        </div>
        <div class="pt-3 border-t border-gray-200 dark:border-gray-800 space-y-2">
          <button id="deleteAllHistoryBtn" class="w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg bg-red-50 dark:bg-red-900/30 text-red-600 dark:text-red-400 hover:bg-red-100 dark:hover:bg-red-900/50 font-medium text-sm">
            <i class="fas fa-trash-can"></i> <span data-i18n="delete_all_history">Xoá toàn bộ lịch sử</span>
          </button>
          <button id="clearMemoriesBtn" class="w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg bg-purple-50 dark:bg-purple-900/30 text-purple-600 dark:text-purple-400 hover:bg-purple-100 dark:hover:bg-purple-900/50 font-medium text-sm">
            <i class="fas fa-brain"></i> <span data-i18n="clear_my_memories">Xoá bộ nhớ AI của tôi</span>
          </button>
        </div>
      </div>
    </div>

    <div id="helpModal" class="modal-card hidden bg-white dark:bg-gray-900 rounded-2xl shadow-2xl w-full max-w-md max-h-[85vh] overflow-y-auto">
      <div class="flex items-center justify-between px-5 py-4 border-b border-gray-200 dark:border-gray-800">
        <h3 class="font-semibold text-lg" data-i18n="help">Trợ giúp &amp; phím tắt</h3>
        <button class="modal-close-btn text-gray-400 hover:text-gray-600 w-8 h-8 flex items-center justify-center"><i class="fas fa-xmark"></i></button>
      </div>
      <div class="p-5 space-y-3 text-sm">
        <div class="flex items-center justify-between"><span data-i18n="shortcut_new_chat">Đoạn chat mới</span><kbd class="px-2 py-1 rounded bg-gray-100 dark:bg-gray-800 text-xs font-mono">Ctrl/Cmd + K</kbd></div>
        <div class="flex items-center justify-between"><span data-i18n="shortcut_help">Mở trợ giúp</span><kbd class="px-2 py-1 rounded bg-gray-100 dark:bg-gray-800 text-xs font-mono">Ctrl/Cmd + /</kbd></div>
        <div class="flex items-center justify-between"><span data-i18n="shortcut_close">Đóng hộp thoại</span><kbd class="px-2 py-1 rounded bg-gray-100 dark:bg-gray-800 text-xs font-mono">Esc</kbd></div>
        <div class="flex items-center justify-between"><span data-i18n="shortcut_send">Gửi câu hỏi</span><kbd class="px-2 py-1 rounded bg-gray-100 dark:bg-gray-800 text-xs font-mono">Enter</kbd></div>
      </div>
    </div>

    <div id="upgradeModal" class="modal-card hidden bg-white dark:bg-gray-900 rounded-2xl shadow-2xl w-full max-w-md max-h-[85vh] overflow-y-auto">
      <div class="flex items-center justify-between px-5 py-4 border-b border-gray-200 dark:border-gray-800">
        <h3 class="font-semibold text-lg" data-i18n="upgrade">Nâng cấp gói</h3>
        <button class="modal-close-btn text-gray-400 hover:text-gray-600 w-8 h-8 flex items-center justify-center"><i class="fas fa-xmark"></i></button>
      </div>
      <div class="p-5 text-sm text-gray-500 dark:text-gray-400 space-y-3">
        <p data-i18n="upgrade_preview">Tính năng nâng cấp gói đang được xây dựng và hiện chưa hỗ trợ thanh toán. Đây chỉ là bản xem trước giao diện.</p>
      </div>
    </div>

    <div id="reportIssueModal" class="modal-card hidden bg-white dark:bg-gray-900 rounded-2xl shadow-2xl w-full max-w-md max-h-[85vh] overflow-y-auto">
      <div class="flex items-center justify-between px-5 py-4 border-b border-gray-200 dark:border-gray-800">
        <h3 class="font-semibold text-lg" data-i18n="report_issue">Báo lỗi câu trả lời</h3>
        <button class="modal-close-btn text-gray-400 hover:text-gray-600 w-8 h-8 flex items-center justify-center"><i class="fas fa-xmark"></i></button>
      </div>
      <div class="p-5 space-y-3 text-sm">
        <p class="text-xs text-gray-400" data-i18n="report_issue_desc">Cho Thầy/Cô biết câu trả lời này có vấn đề gì (sai kiến thức, khó hiểu, lạc đề...) để đội ngũ StudyMate cải thiện AI nhé.</p>
        <textarea id="reportIssueText" rows="4" maxlength="1000" data-i18n-placeholder="report_issue_placeholder" placeholder="Mô tả vấn đề em gặp phải..."
          class="w-full px-3 py-2 rounded-lg bg-gray-100 dark:bg-gray-800 border-0 focus:outline-none focus:ring-2 focus:ring-red-500 dark:text-white resize-none"></textarea>
        <button id="reportIssueSubmitBtn" class="w-full px-4 py-2.5 rounded-lg bg-red-600 hover:bg-red-700 disabled:opacity-50 text-white text-sm font-medium" data-i18n="send_report">Gửi báo cáo</button>
        <p id="reportIssueStatus" class="text-xs text-green-600 hidden" data-i18n="report_sent">Đã gửi báo cáo, cảm ơn em! ✓</p>
      </div>
    </div>

  </div>

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

      <div class="flex-1"></div>

      <button onclick="startVoice()" class="w-9 h-9 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-600 dark:text-gray-300 flex items-center justify-center" title="Trợ lý giọng nói">
        <i class="fas fa-microphone"></i>
      </button>
      <button onclick="toggleTheme()" class="w-9 h-9 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-600 dark:text-gray-300 flex items-center justify-center" title="Đổi giao diện">
        <i id="themeIcon" class="fas fa-moon"></i>
      </button>
    </header>

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

<script>
const CURRENT_USERNAME = {{ username|tojson }};
let PREFERENCES = {{ preferences|tojson }};
const INITIAL_BANNER = {{ banner|tojson }};
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

// ---------- i18n (nhẹ) ----------
const I18N = {
  vi: {
    new_chat: 'Đoạn chat mới', search_chats: 'Tìm đoạn chat...', projects: 'Dự án', pinned: 'Đã ghim',
    recent: 'Gần đây', settings: 'Cài đặt', help: 'Trợ giúp & phím tắt', upgrade: 'Nâng cấp gói',
    logout: 'Đăng xuất', theme: 'Giao diện', theme_light: 'Sáng', theme_dark: 'Tối', theme_system: 'Theo hệ thống',
    language: 'Ngôn ngữ', default_subject: 'Môn học mặc định', default_mode: 'Chế độ mặc định', none: 'Không',
    delete_all_history: 'Xoá toàn bộ lịch sử', shortcut_new_chat: 'Đoạn chat mới', shortcut_help: 'Mở trợ giúp',
    shortcut_close: 'Đóng hộp thoại', shortcut_send: 'Gửi câu hỏi',
    upgrade_preview: 'Tính năng nâng cấp gói đang được xây dựng và hiện chưa hỗ trợ thanh toán. Đây chỉ là bản xem trước giao diện.',
    clear_my_memories: 'Xoá bộ nhớ AI của tôi', report_issue: 'Báo lỗi câu trả lời',
    report_issue_desc: 'Cho Thầy/Cô biết câu trả lời này có vấn đề gì (sai kiến thức, khó hiểu, lạc đề...) để đội ngũ StudyMate cải thiện AI nhé.',
    report_issue_placeholder: 'Mô tả vấn đề em gặp phải...', send_report: 'Gửi báo cáo',
    report_sent: 'Đã gửi báo cáo, cảm ơn em! ✓', report_btn: 'Báo lỗi',
  },
  en: {
    new_chat: 'New chat', search_chats: 'Search chats...', projects: 'Projects', pinned: 'Pinned',
    recent: 'Recent', settings: 'Settings', help: 'Help & shortcuts', upgrade: 'Upgrade plan',
    logout: 'Log out', theme: 'Theme', theme_light: 'Light', theme_dark: 'Dark', theme_system: 'System',
    language: 'Language', default_subject: 'Default subject', default_mode: 'Default mode', none: 'None',
    delete_all_history: 'Delete all history', shortcut_new_chat: 'New chat', shortcut_help: 'Open help',
    shortcut_close: 'Close dialog', shortcut_send: 'Send message',
    upgrade_preview: 'The upgrade flow is a UI preview only — no billing is implemented yet.',
    clear_my_memories: 'Clear my AI memories', report_issue: 'Report an answer',
    report_issue_desc: 'Tell us what went wrong with this answer (wrong info, confusing, off-topic...) so we can improve the AI.',
    report_issue_placeholder: 'Describe the problem...', send_report: 'Send report',
    report_sent: 'Report sent, thank you! ✓', report_btn: 'Report',
  },
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

// ---------- Theme (đồng bộ với preferences) ----------
function applyTheme(mode) {
  const icon = document.getElementById('themeIcon');
  const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  const isDark = mode === 'dark' || (mode === 'system' && prefersDark);
  html.classList.toggle('dark', isDark);
  if (icon) { icon.classList.toggle('fa-sun', isDark); icon.classList.toggle('fa-moon', !isDark); }
}
function toggleTheme() {
  const isDark = html.classList.contains('dark');
  const newMode = isDark ? 'light' : 'dark';
  PREFERENCES.theme = newMode;
  applyTheme(newMode);
  savePreferences({ theme: newMode });
  const sel = document.getElementById('settingTheme');
  if (sel) sel.value = newMode;
}

async function savePreferences(updates) {
  try {
    const res = await fetch('/api/preferences', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(updates)
    });
    if (res.ok) PREFERENCES = await res.json();
  } catch (e) { /* im lặng bỏ qua lỗi mạng */ }
}

function applyPreferencesToUI() {
  applyTheme(PREFERENCES.theme || 'system');
  applyLanguage(PREFERENCES.language || 'vi');
  const themeSel = document.getElementById('settingTheme');
  const langSel = document.getElementById('settingLanguage');
  const subjSel = document.getElementById('settingDefaultSubject');
  const modeSel = document.getElementById('settingDefaultMode');
  if (themeSel) themeSel.value = PREFERENCES.theme || 'system';
  if (langSel) langSel.value = PREFERENCES.language || 'vi';
  if (subjSel) subjSel.value = PREFERENCES.default_subject || '';
  if (modeSel) modeSel.value = PREFERENCES.default_mode || '';
  if (PREFERENCES.default_subject) {
    const s = document.getElementById('subject');
    if (s && [...s.options].some(o => o.value === PREFERENCES.default_subject)) s.value = PREFERENCES.default_subject;
  }
  if (PREFERENCES.default_mode) {
    const m = document.getElementById('modeSelect');
    if (m && [...m.options].some(o => o.value === PREFERENCES.default_mode)) m.value = PREFERENCES.default_mode;
  }
}
applyPreferencesToUI();
if (window.matchMedia) {
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (PREFERENCES.theme === 'system') applyTheme('system');
  });
}

document.getElementById('settingTheme').addEventListener('change', (e) => {
  PREFERENCES.theme = e.target.value; applyTheme(e.target.value); savePreferences({ theme: e.target.value });
});
document.getElementById('settingLanguage').addEventListener('change', (e) => {
  PREFERENCES.language = e.target.value; applyLanguage(e.target.value); savePreferences({ language: e.target.value });
});
document.getElementById('settingDefaultSubject').addEventListener('change', (e) => {
  PREFERENCES.default_subject = e.target.value; savePreferences({ default_subject: e.target.value });
});
document.getElementById('settingDefaultMode').addEventListener('change', (e) => {
  PREFERENCES.default_mode = e.target.value; savePreferences({ default_mode: e.target.value });
});
document.getElementById('deleteAllHistoryBtn').addEventListener('click', async () => {
  const msg = (I18N[PREFERENCES.language || 'vi'].delete_all_history) + '?';
  if (!confirm(msg + ' ' + (PREFERENCES.language === 'en' ? 'This cannot be undone.' : 'Hành động này không thể hoàn tác.'))) return;
  try {
    await fetch('/api/conversations/delete-all', { method: 'POST' });
    newChat();
  } catch (e) { /* im lặng bỏ qua lỗi mạng */ }
});

document.getElementById('clearMemoriesBtn').addEventListener('click', async () => {
  const isEn = PREFERENCES.language === 'en';
  if (!confirm(isEn ? 'Clear all AI memories about you? This cannot be undone.' : 'Xoá toàn bộ bộ nhớ AI về em? Hành động này không thể hoàn tác.')) return;
  try {
    await fetch('/api/memories', { method: 'DELETE' });
    alert(isEn ? 'Cleared.' : 'Đã xoá xong.');
  } catch (e) { /* im lặng bỏ qua lỗi mạng */ }
});

document.getElementById('reportIssueSubmitBtn').addEventListener('click', async () => {
  const textEl = document.getElementById('reportIssueText');
  const statusEl = document.getElementById('reportIssueStatus');
  const description = textEl.value.trim();
  if (!description) { textEl.focus(); return; }
  const btn = document.getElementById('reportIssueSubmitBtn');
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
      setTimeout(closeModal, 1200);
    } else {
      const data = await res.json().catch(() => ({}));
      alert(data.error || 'Không gửi được báo cáo.');
    }
  } catch (err) {
    alert('Lỗi mạng khi gửi báo cáo.');
  } finally {
    btn.disabled = false;
  }
});

// ---------- Modal system ---------- 
const modalOverlay = document.getElementById('modalOverlay');
function openModal(id) {
  modalOverlay.classList.add('open');
  document.querySelectorAll('.modal-card').forEach(c => c.classList.remove('open'));
  document.getElementById(id).classList.add('open');
}
function closeModal() {
  modalOverlay.classList.remove('open');
  document.querySelectorAll('.modal-card').forEach(c => c.classList.remove('open'));
}
modalOverlay.addEventListener('click', (e) => { if (e.target === modalOverlay) closeModal(); });
document.querySelectorAll('.modal-close-btn').forEach(btn => btn.addEventListener('click', closeModal));
document.getElementById('openSettingsBtn').addEventListener('click', () => { userMenu.classList.add('hidden'); openModal('settingsModal'); });
document.getElementById('openHelpBtn').addEventListener('click', () => { userMenu.classList.add('hidden'); openModal('helpModal'); });
document.getElementById('openUpgradeBtn').addEventListener('click', () => { userMenu.classList.add('hidden'); openModal('upgradeModal'); });

// ---------- Phím tắt ----------
document.addEventListener('keydown', (e) => {
  const cmd = e.ctrlKey || e.metaKey;
  if (cmd && e.key.toLowerCase() === 'k') { e.preventDefault(); newChat(); }
  else if (cmd && e.key === '/') { e.preventDefault(); openModal('helpModal'); }
  else if (e.key === 'Escape') { closeModal(); }
});

// ---------- Banner hệ thống ----------
function renderBanner(banner) {
  const el = document.getElementById('systemBanner');
  const dismissedText = sessionStorage.getItem('bannerDismissedText');
  if (banner && banner.active && banner.text && banner.text !== dismissedText) {
    document.getElementById('systemBannerText').textContent = banner.text;
    el.classList.remove('hidden');
  } else {
    el.classList.add('hidden');
  }
}
renderBanner(INITIAL_BANNER);
document.getElementById('systemBannerClose').addEventListener('click', () => {
  const text = document.getElementById('systemBannerText').textContent;
  sessionStorage.setItem('bannerDismissedText', text);
  document.getElementById('systemBanner').classList.add('hidden');
});

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
  wrapper.innerHTML = `<div class="w-8 h-8 rounded-full bg-gradient-to-br from-blue-600 to-indigo-600 flex items-center justify-center text-white text-sm flex-shrink-0 mt-0.5"><i class="fas fa-robot"></i></div>
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
  avatar.className = 'w-8 h-8 rounded-full bg-gradient-to-br from-blue-600 to-indigo-600 flex items-center justify-center text-white text-sm flex-shrink-0 mt-0.5';
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
  wrapper.innerHTML = `<div class="w-8 h-8 rounded-full bg-gradient-to-br from-blue-600 to-indigo-600 flex items-center justify-center text-white text-sm flex-shrink-0 mt-0.5"><i class="fas fa-robot"></i></div>
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

// ---------- Hành động dưới câu trả lời AI (Báo lỗi...) ----------
function addMessageActions(wrapper, conversationId, getText) {
  const bar = document.createElement('div');
  bar.className = 'msg-actions flex items-center gap-1 mt-1 ml-11';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'text-xs text-gray-400 hover:text-red-500 px-2 py-1 -ml-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 flex items-center gap-1.5';
  btn.title = 'Báo lỗi câu trả lời này';
  btn.innerHTML = '<i class="fas fa-flag"></i> <span data-i18n="report_btn">Báo lỗi</span>';
  btn.addEventListener('click', () => openReportModal(conversationId, getText()));
  bar.appendChild(btn);
  wrapper.after(bar);
  applyLanguage(PREFERENCES.language || 'vi');
  return bar;
}

let reportContext = { conversationId: null, messageExcerpt: '' };
function openReportModal(conversationId, messageExcerpt) {
  reportContext = { conversationId: conversationId || null, messageExcerpt: (messageExcerpt || '').slice(0, 2000) };
  const textEl = document.getElementById('reportIssueText');
  const statusEl = document.getElementById('reportIssueStatus');
  textEl.value = '';
  statusEl.classList.add('hidden');
  openModal('reportIssueModal');
  setTimeout(() => textEl.focus(), 50);
}

function showMemoryToast(text) {
  const toast = document.createElement('div');
  toast.className = 'memory-toast fixed bottom-24 left-1/2 z-50 bg-gray-900 dark:bg-gray-100 text-white dark:text-gray-900 text-xs px-4 py-2 rounded-full shadow-lg flex items-center gap-2 max-w-[90vw]';
  toast.innerHTML = `<i class="fas fa-brain text-purple-400"></i> <span class="truncate">Đã ghi nhớ: ${escapeHtml(text.slice(0, 80))}</span>`;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 3600);
}

function showWelcome() {
  addMessage('ai', '👋 Chào em! Thầy/Cô là **StudyMate AI Pro**.\n\nEm chọn **Môn học** và **Chế độ** ở phía trên, gõ câu hỏi rồi bấm Enter (hoặc nút gửi) nhé! Em cũng có thể đính kèm file (PDF/Word/txt/csv) hoặc ảnh bằng nút 📎, hay kéo-thả trực tiếp vào khung chat. 🚀', true);
}

// ---------- Dự án (Projects) ----------
let PROJECTS = [];
async function loadProjects() {
  try {
    const res = await fetch('/api/projects');
    if (!res.ok) return;
    PROJECTS = await res.json();
    renderProjectList();
  } catch (e) { /* im lặng bỏ qua lỗi mạng */ }
}
function renderProjectList() {
  const container = document.getElementById('projectList');
  container.innerHTML = '';
  PROJECTS.forEach(p => {
    const item = document.createElement('div');
    item.className = 'flex items-center gap-2 rounded-xl px-3 py-1.5 hover:bg-gray-200 dark:hover:bg-gray-800 cursor-pointer text-gray-600 dark:text-gray-300';
    item.innerHTML = `<i class="fas fa-folder text-xs text-gray-400"></i><span class="flex-1 truncate">${escapeHtml(p.name)}</span>`;
    item.addEventListener('click', () => { activeProjectFilter = (activeProjectFilter === p.id ? null : p.id); loadConversations(); });
    container.appendChild(item);
  });
}
document.getElementById('newProjectBtn').addEventListener('click', async () => {
  const name = prompt(PREFERENCES.language === 'en' ? 'Project name:' : 'Tên dự án:');
  if (!name || !name.trim()) return;
  try {
    const res = await fetch('/api/projects', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name.trim() })
    });
    if (res.ok) loadProjects();
  } catch (e) { /* im lặng bỏ qua lỗi mạng */ }
});

// ---------- Lịch sử hội thoại (theo tài khoản) ----------
let ALL_CONVERSATIONS = [];
let activeProjectFilter = null;
let openConvMenuId = null;

async function loadConversations() {
  try {
    const res = await fetch('/api/conversations');
    if (res.status === 401) { window.location.href = '/login'; return; }
    if (!res.ok) return;
    ALL_CONVERSATIONS = await res.json();
    renderConversationList();
  } catch (e) { /* im lặng bỏ qua lỗi mạng khi tải danh sách */ }
}

function renderConversationList() {
  const query = (document.getElementById('convSearchInput').value || '').trim().toLowerCase();
  let list = ALL_CONVERSATIONS;
  if (query) list = list.filter(c => (c.title || '').toLowerCase().includes(query));
  if (activeProjectFilter) list = list.filter(c => c.project_id === activeProjectFilter);

  const pinned = list.filter(c => c.pinned);
  const recent = list.filter(c => !c.pinned);

  document.getElementById('pinnedSection').classList.toggle('hidden', pinned.length === 0);
  renderConvGroup('pinnedList', pinned);
  renderConvGroup('convList', recent, recent.length === 0 && pinned.length === 0);
}

function renderConvGroup(containerId, list, showEmptyState) {
  const container = document.getElementById(containerId);
  container.innerHTML = '';
  if (!list.length) {
    if (showEmptyState) container.innerHTML = '<div class="text-gray-400 text-xs px-2 py-4 text-center">Chưa có đoạn chat nào</div>';
    return;
  }
  list.forEach(conv => {
    const item = document.createElement('div');
    const active = conv.id === currentConversationId;
    item.className = 'conv-item group flex items-center gap-1 rounded-xl px-3 py-2 cursor-pointer transition-colors ' +
      (active ? 'bg-gray-200 dark:bg-gray-800' : 'hover:bg-gray-200 dark:hover:bg-gray-800');
    item.innerHTML = `
      ${conv.pinned ? '<i class="fas fa-thumbtack text-xs text-gray-400"></i>' : ''}
      <span class="flex-1 truncate">${escapeHtml(conv.title || 'Đoạn chat mới')}</span>
      <button class="conv-menu-btn text-gray-400 hover:text-gray-600 w-6 h-6 flex items-center justify-center flex-shrink-0 transition-opacity" title="Tùy chọn">
        <i class="fas fa-ellipsis text-xs"></i>
      </button>`;
    item.querySelector('span').addEventListener('click', () => openConversation(conv.id));
    item.querySelector('.conv-menu-btn').addEventListener('click', (e) => {
      e.stopPropagation();
      toggleConvMenu(item, conv);
    });
    container.appendChild(item);
  });
}

function toggleConvMenu(item, conv) {
  const existing = item.querySelector('.conv-menu-dropdown');
  document.querySelectorAll('.conv-menu-dropdown').forEach(d => d.remove());
  if (existing) { openConvMenuId = null; return; }
  openConvMenuId = conv.id;
  const dd = document.createElement('div');
  dd.className = 'conv-menu-dropdown';
  const pinLabel = conv.pinned ? (PREFERENCES.language === 'en' ? 'Unpin' : 'Bỏ ghim') : (PREFERENCES.language === 'en' ? 'Pin' : 'Ghim');
  const renameLabel = PREFERENCES.language === 'en' ? 'Rename' : 'Đổi tên';
  const moveLabel = PREFERENCES.language === 'en' ? 'Move to project' : 'Chuyển vào dự án';
  const delLabel = PREFERENCES.language === 'en' ? 'Delete' : 'Xóa';
  dd.innerHTML = `
    <button data-act="pin"><i class="fas fa-thumbtack w-3"></i>${pinLabel}</button>
    <button data-act="rename"><i class="fas fa-pen w-3"></i>${renameLabel}</button>
    ${PROJECTS.length ? `<button data-act="move"><i class="fas fa-folder w-3"></i>${moveLabel}</button>` : ''}
    <button data-act="delete" class="text-red-500"><i class="fas fa-trash-can w-3"></i>${delLabel}</button>`;
  dd.querySelector('[data-act="pin"]').addEventListener('click', (e) => { e.stopPropagation(); pinConversation(conv); });
  dd.querySelector('[data-act="rename"]').addEventListener('click', (e) => { e.stopPropagation(); renameConversation(conv); });
  const moveBtn = dd.querySelector('[data-act="move"]');
  if (moveBtn) moveBtn.addEventListener('click', (e) => { e.stopPropagation(); moveConversation(conv); });
  dd.querySelector('[data-act="delete"]').addEventListener('click', (e) => { e.stopPropagation(); deleteConversation(conv.id); });
  item.appendChild(dd);
}
document.addEventListener('click', () => { document.querySelectorAll('.conv-menu-dropdown').forEach(d => d.remove()); openConvMenuId = null; });

async function patchConversation(id, updates) {
  try {
    const res = await fetch(`/api/conversations/${id}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(updates)
    });
    if (res.ok) loadConversations();
  } catch (e) { /* im lặng bỏ qua lỗi mạng */ }
}
function pinConversation(conv) { patchConversation(conv.id, { pinned: !conv.pinned }); }
function renameConversation(conv) {
  const title = prompt(PREFERENCES.language === 'en' ? 'New title:' : 'Tên mới:', conv.title || '');
  if (title === null || !title.trim()) return;
  patchConversation(conv.id, { title: title.trim() });
}
function moveConversation(conv) {
  const options = PROJECTS.map((p, i) => `${i + 1}. ${p.name}`).join('\n');
  const choice = prompt((PREFERENCES.language === 'en' ? 'Move to which project?\n' : 'Chuyển vào dự án nào?\n') + options);
  if (!choice) return;
  const idx = parseInt(choice, 10) - 1;
  if (isNaN(idx) || !PROJECTS[idx]) return;
  patchConversation(conv.id, { project_id: PROJECTS[idx].id });
}

document.getElementById('convSearchInput').addEventListener('input', renderConversationList);

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
loadProjects();

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
        conversationId: currentConversationId
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
  loadConversations();
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
    <title>Bảo mật ứng dụng StudyMate AI Pro với HTTPS</title>
    <script src="https://cdn.tailwindcss.com"></script>
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
            <h1 class="text-4xl font-extrabold text-gray-900 mb-4">Nâng cấp Bảo mật cho StudyMate AI Pro</h1>
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
            <p class="mb-4">Báo cáo được thực hiện cho dự án StudyMate AI Pro</p>
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
  <title>Thống kê sử dụng — StudyMate AI Pro</title>
  <script src="https://cdn.tailwindcss.com"></script>
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

    {% if open_issues_count > 0 %}
    <a href="#issue-reports" class="block bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-900/50 rounded-2xl px-5 py-3 text-sm font-medium text-red-700 dark:text-red-300 flex items-center gap-2 hover:bg-red-100 dark:hover:bg-red-900/30 transition-colors">
      <i class="fas fa-flag"></i> Có {{ open_issues_count }} báo cáo lỗi đang chờ xử lý — xem bên dưới ↓
    </a>
    {% endif %}

    <!-- Thẻ tổng quan -->
    <div class="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-5 gap-4">
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
      <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm">
        <div class="flex items-center justify-between">
          <span class="text-xs font-semibold text-gray-400 uppercase">Báo lỗi đang mở</span>
          <i class="fas fa-flag text-red-500"></i>
        </div>
        <p class="text-3xl font-extrabold mt-2">{{ open_issues_count }}</p>
        <p class="text-xs text-gray-400 mt-1">{{ total_issues_count }} báo cáo tổng cộng</p>
      </div>
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
    <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm">
      <h2 class="font-bold mb-4 flex items-center gap-2"><i class="fas fa-sliders text-gray-500"></i> Quản lý hệ thống</h2>

      <div class="grid md:grid-cols-2 gap-6">
        <div>
          <div class="text-sm font-semibold mb-2">Banner thông báo toàn hệ thống</div>
          <form id="bannerForm" class="space-y-2">
            <textarea id="bannerTextInput" rows="2" maxlength="300" placeholder="Nội dung thông báo hiển thị cho mọi người dùng..."
              class="w-full px-3 py-2 text-sm rounded-lg bg-gray-100 dark:bg-gray-800 border-0 focus:outline-none focus:ring-2 focus:ring-indigo-500 dark:text-white">{{ banner.text }}</textarea>
            <label class="flex items-center gap-2 text-sm">
              <input type="checkbox" id="bannerActiveInput" {% if banner.active %}checked{% endif %}>
              Bật banner
            </label>
            <button type="submit" class="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-medium">Lưu banner</button>
            <span id="bannerSaveStatus" class="text-xs text-green-600 ml-2 hidden">Đã lưu ✓</span>
          </form>
        </div>

        <div>
          <div class="text-sm font-semibold mb-2">Đăng nhập Google (runtime)</div>
          {% if google_oauth_configured %}
          <p class="text-xs text-gray-400 mb-2">Client ID/Secret đã cấu hình trong .env. Bạn có thể tạm tắt nút "Đăng nhập với Google" mà không cần sửa .env.</p>
          <div class="flex gap-2">
            <button data-mode="on" class="google-toggle-btn px-3 py-2 rounded-lg text-sm font-medium {{ 'bg-green-600 text-white' if google_login_on else 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300' }}">Bật</button>
            <button data-mode="off" class="google-toggle-btn px-3 py-2 rounded-lg text-sm font-medium {{ 'bg-red-600 text-white' if not google_login_on else 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300' }}">Tắt</button>
            <button data-mode="default" class="google-toggle-btn px-3 py-2 rounded-lg text-sm font-medium bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300">Mặc định (.env)</button>
          </div>
          <p class="text-xs text-gray-400 mt-2">Trạng thái hiện tại: <strong>{{ 'BẬT' if google_login_on else 'TẮT' }}</strong></p>
          {% else %}
          <p class="text-xs text-gray-400">Chưa cấu hình GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET trong .env — không có gì để bật/tắt.</p>
          {% endif %}

          <div class="mt-5">
            <div class="text-sm font-semibold mb-2">Xuất dữ liệu</div>
            <a href="/developer/export.csv" class="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 text-sm font-medium">
              <i class="fas fa-file-csv"></i> Tải usage_logs.csv
            </a>
          </div>
        </div>
      </div>
    </div>

    <!-- Báo cáo lỗi từ học sinh -->
    <div id="issue-reports" class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm scroll-mt-20">
      <div class="flex items-center justify-between mb-1 flex-wrap gap-2">
        <h2 class="font-bold flex items-center gap-2"><i class="fas fa-flag text-red-500"></i> Báo cáo lỗi từ học sinh</h2>
        <span class="text-xs text-gray-400">{{ open_issues_count }} đang mở / {{ total_issues_count }} tổng cộng</span>
      </div>
      <p class="text-xs text-gray-400 mb-4">Học sinh bấm "Báo lỗi" dưới 1 câu trả lời trong khung chat để gửi báo cáo về đây.</p>
      <div class="space-y-3">
        {% for r in issue_reports %}
        <div class="border border-gray-100 dark:border-gray-800 rounded-xl p-4 {{ 'opacity-50' if r.status == 'resolved' else '' }}" data-issue-id="{{ r.id }}">
          <div class="flex items-start justify-between gap-3 flex-wrap">
            <div class="min-w-0">
              <div class="flex items-center gap-2 text-xs text-gray-400 mb-1 flex-wrap">
                <span class="font-semibold text-gray-600 dark:text-gray-300">{{ r.username }}</span>
                <span>•</span><span>{{ r.created_at }}</span>
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
            <button class="issue-resolve-btn flex-shrink-0 text-xs font-medium px-3 py-1.5 rounded-lg bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 whitespace-nowrap">
              {{ 'Mở lại' if r.status == 'resolved' else 'Đánh dấu đã xử lý' }}
            </button>
          </div>
        </div>
        {% else %}
        <p class="text-sm text-gray-400">Chưa có báo cáo lỗi nào. 🎉</p>
        {% endfor %}
      </div>
    </div>

    <!-- Bộ nhớ AI -->
    <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm overflow-x-auto">
      <h2 class="font-bold mb-1 flex items-center gap-2"><i class="fas fa-brain text-purple-500"></i> Bộ nhớ AI gần đây ({{ total_memories }} tổng)</h2>
      <p class="text-xs text-gray-400 mb-4">Những điều học sinh chủ động nhờ AI ghi nhớ (vd: "ghi nhớ giúp em là...") hoặc hệ thống tự
        nhận diện (vd: lớp học) — dùng để cá nhân hoá câu trả lời ở các lượt chat sau.</p>
      <div class="space-y-2.5">
        {% for m in recent_memories_admin %}
        <div class="flex items-start gap-3 text-sm border-b border-gray-50 dark:border-gray-900 pb-2.5">
          <div class="w-6 h-6 rounded-full bg-purple-100 dark:bg-purple-900 text-purple-600 dark:text-purple-300 flex items-center justify-center text-xs font-bold flex-shrink-0 mt-0.5">{{ m.username[0]|upper if m.username != '—' else '?' }}</div>
          <div class="min-w-0 flex-1">
            <p class="break-words"><span class="font-semibold">{{ m.username }}</span> — {{ m.content }}</p>
            <p class="text-xs text-gray-400 mt-0.5">{{ m.created_at }} · {{ 'tự động nhận diện' if m.source == 'auto' else 'học sinh yêu cầu' }}</p>
          </div>
        </div>
        {% else %}
        <p class="text-sm text-gray-400">Chưa có bộ nhớ nào được ghi nhận.</p>
        {% endfor %}
      </div>
    </div>

    <!-- Toàn bộ tài khoản -->
    <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm overflow-x-auto">
      <div class="flex items-center justify-between mb-4 flex-wrap gap-2">
        <h2 class="font-bold flex items-center gap-2"><i class="fas fa-address-card text-gray-500"></i> Toàn bộ tài khoản ({{ total_users }})</h2>
        <form method="GET" class="flex items-center gap-2">
          <input type="text" name="q" value="{{ search_q }}" placeholder="Tìm theo tên đăng nhập..."
            class="px-3 py-1.5 text-sm rounded-lg bg-gray-100 dark:bg-gray-800 border-0 focus:outline-none focus:ring-2 focus:ring-indigo-500 dark:text-white">
          <button type="submit" class="px-3 py-1.5 rounded-lg bg-gray-100 dark:bg-gray-800 text-sm"><i class="fas fa-magnifying-glass"></i></button>
        </form>
      </div>
      <table class="w-full text-sm min-w-[520px]">
        <thead>
          <tr class="text-left text-gray-400 text-xs uppercase border-b border-gray-100 dark:border-gray-800">
            <th class="py-2 pr-3">ID</th>
            <th class="py-2 pr-3">Người dùng</th>
            <th class="py-2 pr-3">Vai trò</th>
            <th class="py-2 pr-3">Ngày tạo</th>
            <th class="py-2">Hành động</th>
          </tr>
        </thead>
        <tbody>
          {% for u in all_users %}
          <tr class="border-b border-gray-50 dark:border-gray-900" data-user-id="{{ u.id }}" data-username="{{ u.username }}">
            <td class="py-2.5 pr-3 text-gray-400">#{{ u.id }}</td>
            <td class="py-2.5 pr-3 font-medium">{{ u.username }}</td>
            <td class="py-2.5 pr-3 role-cell">
              {% if u.role == 'developer' %}
                <span class="text-[11px] font-semibold px-2 py-0.5 rounded-full bg-indigo-100 dark:bg-indigo-900 text-indigo-600 dark:text-indigo-300">developer</span>
              {% else %}
                <span class="text-[11px] font-semibold px-2 py-0.5 rounded-full bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400">user</span>
              {% endif %}
            </td>
            <td class="py-2.5 pr-3 text-gray-400">{{ u.created_at[:10] }}</td>
            <td class="py-2.5">
              {% if u.username != current_username %}
              <button class="role-toggle-btn text-xs font-medium px-2.5 py-1 rounded-lg bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700"
                      data-current-role="{{ u.role }}">
                {{ 'Hạ xuống user' if u.role == 'developer' else 'Nâng lên developer' }}
              </button>
              {% else %}
              <span class="text-xs text-gray-400">(bạn)</span>
              {% endif %}
            </td>
          </tr>
          {% else %}
          <tr><td colspan="5" class="py-4 text-center text-gray-400">Không tìm thấy tài khoản nào.</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </div>

    <p class="text-center text-xs text-gray-400 pb-4">
      Trang này chỉ hiển thị số liệu tổng hợp (số lượt, độ dài, môn học, chế độ) — không lưu/hiển thị nội dung câu hỏi hay câu trả lời của học sinh.
    </p>
  </main>

<script>
  document.getElementById('bannerForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const text = document.getElementById('bannerTextInput').value.trim();
    const active = document.getElementById('bannerActiveInput').checked;
    try {
      const res = await fetch('/developer/banner', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, active })
      });
      if (res.ok) {
        const status = document.getElementById('bannerSaveStatus');
        status.classList.remove('hidden');
        setTimeout(() => status.classList.add('hidden'), 2000);
      } else {
        alert('Không lưu được banner.');
      }
    } catch (err) { alert('Lỗi mạng khi lưu banner.'); }
  });

  document.querySelectorAll('.google-toggle-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      try {
        const res = await fetch('/developer/google-login', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mode: btn.dataset.mode })
        });
        if (res.ok) window.location.reload();
        else alert('Không đổi được trạng thái đăng nhập Google.');
      } catch (err) { alert('Lỗi mạng.'); }
    });
  });

  document.querySelectorAll('.issue-resolve-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const card = btn.closest('[data-issue-id]');
      const id = card.dataset.issueId;
      btn.disabled = true;
      try {
        const res = await fetch(`/developer/issues/${id}/resolve`, { method: 'POST' });
        if (res.ok) window.location.reload();
        else { alert('Không cập nhật được trạng thái báo cáo.'); btn.disabled = false; }
      } catch (err) { alert('Lỗi mạng.'); btn.disabled = false; }
    });
  });

  document.querySelectorAll('.role-toggle-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const row = btn.closest('tr');
      const userId = row.dataset.userId;
      const username = row.dataset.username;
      const currentRole = btn.dataset.currentRole;
      const newRole = currentRole === 'developer' ? 'user' : 'developer';
      const confirmMsg = newRole === 'developer'
        ? `Nâng "${username}" lên quyền developer?`
        : `Hạ quyền developer của "${username}" xuống user?`;
      if (!confirm(confirmMsg)) return;
      try {
        const res = await fetch(`/developer/users/${userId}/role`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ role: newRole })
        });
        const data = await res.json();
        if (!res.ok) { alert(data.error || 'Không cập nhật được vai trò.'); return; }
        window.location.reload();
      } catch (err) { alert('Lỗi mạng khi cập nhật vai trò.'); }
    });
  });
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
    session['role'] = user['role'] if 'role' in user.keys() else 'user'


@app.route('/auth/<provider>')
def oauth_start(provider):
    if provider != 'google' or not oauth or not hasattr(oauth, provider) or not google_login_effective():
        flash('Phương thức đăng nhập này hiện chưa được bật.')
        return redirect(url_for('login_page'))
    redirect_uri = url_for('oauth_callback', provider=provider, _external=True)
    client = getattr(oauth, provider)
    return client.authorize_redirect(redirect_uri)


@app.route('/auth/<provider>/callback')
def oauth_callback(provider):
    if provider != 'google' or not oauth or not hasattr(oauth, provider):
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
    _login_session_for(user)
    return redirect(url_for('home'))


# ==========================================
# 4. ĐỊNH TUYẾN TÀI KHOẢN (Đăng ký / Đăng nhập / Đăng xuất)
# ==========================================
def _auth_ctx(**extra):
    ctx = {'google_enabled': google_login_effective()}
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
                session.clear()
                session['user_id'] = cur.lastrowid
                session['username'] = username
                session['role'] = 'user'
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
    return render_template_string(
        HTML,
        username=session.get('username', ''),
        is_developer=(current_user_role() == 'developer'),
        preferences=get_preferences(current_user_id()),
        banner=get_banner(),
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

    q = (request.args.get('q') or '').strip()
    if q:
        all_users = db.execute(
            'SELECT id, username, role, created_at FROM users WHERE username LIKE ? ORDER BY id ASC',
            (f'%{q}%',)
        ).fetchall()
    else:
        all_users = db.execute(
            'SELECT id, username, role, created_at FROM users ORDER BY id ASC'
        ).fetchall()

    developer_count = db.execute("SELECT COUNT(*) c FROM users WHERE role = 'developer'").fetchone()['c']

    # ---- Báo lỗi từ học sinh ----
    open_issues_count = db.execute("SELECT COUNT(*) c FROM issue_reports WHERE status = 'open'").fetchone()['c']
    total_issues_count = db.execute('SELECT COUNT(*) c FROM issue_reports').fetchone()['c']
    issue_rows = db.execute('''
        SELECT r.id AS id, r.description AS description, r.message_excerpt AS message_excerpt,
               r.status AS status, r.created_at AS created_at,
               u.username AS username
        FROM issue_reports r
        LEFT JOIN users u ON u.id = r.user_id
        ORDER BY (r.status = 'open') DESC, r.created_at DESC
        LIMIT 30
    ''').fetchall()
    issue_reports = []
    for r in issue_rows:
        issue_reports.append({
            'id': r['id'],
            'description': r['description'],
            'message_excerpt': r['message_excerpt'] or '',
            'status': r['status'],
            'username': r['username'] or '—',
            'created_at': r['created_at'][:16].replace('T', ' ') if r['created_at'] else '',
        })

    # ---- Bộ nhớ AI (memories) — tổng hợp cho developer xem ----
    total_memories = db.execute('SELECT COUNT(*) c FROM memories').fetchone()['c']
    memory_rows = db.execute('''
        SELECT m.content AS content, m.source AS source, m.created_at AS created_at,
               u.username AS username
        FROM memories m
        LEFT JOIN users u ON u.id = m.user_id
        ORDER BY m.created_at DESC
        LIMIT 10
    ''').fetchall()
    recent_memories_admin = []
    for r in memory_rows:
        recent_memories_admin.append({
            'content': r['content'],
            'source': r['source'],
            'username': r['username'] or '—',
            'created_at': r['created_at'][:16].replace('T', ' ') if r['created_at'] else '',
        })

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
        daily_counts=daily_counts,
        subject_stats=subject_stats,
        mode_stats=mode_stats,
        top_users=top_users,
        all_users=[dict(u) for u in all_users],
        search_q=q,
        current_username=session.get('username', ''),
        developer_count=developer_count,
        banner=get_banner(),
        google_oauth_configured=GOOGLE_OAUTH_ENABLED,
        google_login_on=google_login_effective(),
        open_issues_count=open_issues_count,
        total_issues_count=total_issues_count,
        issue_reports=issue_reports,
        total_memories=total_memories,
        recent_memories_admin=recent_memories_admin,
    )


@app.route('/developer/users/<int:user_id>/role', methods=['POST'])
@developer_required
def developer_set_role(user_id):
    """Thăng/hạ quyền developer cho 1 tài khoản. Chặn: tự hạ quyền chính mình,
    và hạ quyền developer cuối cùng của hệ thống (luôn phải còn ít nhất 1 developer)."""
    db = get_db()
    target = db.execute('SELECT id, username, role FROM users WHERE id = ?', (user_id,)).fetchone()
    if not target:
        return jsonify({"error": "Không tìm thấy tài khoản này."}), 404

    data = request.get_json(silent=True) or {}
    new_role = data.get('role')
    if new_role not in ('user', 'developer'):
        return jsonify({"error": "Vai trò không hợp lệ."}), 400

    if target['role'] == new_role:
        return jsonify({"success": True, "role": new_role})

    if new_role == 'user':
        if target['id'] == current_user_id():
            return jsonify({"error": "Bạn không thể tự hạ quyền chính mình."}), 400
        dev_count = db.execute("SELECT COUNT(*) c FROM users WHERE role = 'developer'").fetchone()['c']
        if target['role'] == 'developer' and dev_count <= 1:
            return jsonify({"error": "Không thể hạ quyền — hệ thống cần ít nhất 1 tài khoản developer."}), 400

    db.execute('UPDATE users SET role = ? WHERE id = ?', (new_role, user_id))
    db.commit()
    return jsonify({"success": True, "role": new_role})


@app.route('/developer/banner', methods=['POST'])
@developer_required
def developer_set_banner():
    data = request.get_json(silent=True) or {}
    text = (data.get('text') or '').strip()[:300]
    active = bool(data.get('active'))
    set_setting('banner_text', text)
    set_setting('banner_active', '1' if active else '0')
    return jsonify({"success": True, "banner": get_banner()})


@app.route('/developer/google-login', methods=['POST'])
@developer_required
def developer_set_google_login():
    data = request.get_json(silent=True) or {}
    mode = data.get('mode')
    if mode not in ('on', 'off', 'default'):
        return jsonify({"error": "Giá trị không hợp lệ."}), 400
    if mode == 'default':
        set_setting('google_login_override', '')
    else:
        set_setting('google_login_override', mode)
    return jsonify({"success": True, "google_login_on": google_login_effective()})


@app.route('/developer/issues/<int:issue_id>/resolve', methods=['POST'])
@developer_required
def developer_resolve_issue(issue_id):
    """Đánh dấu 1 báo cáo lỗi là đã xử lý (hoặc mở lại nếu bấm lần nữa)."""
    db = get_db()
    row = db.execute('SELECT id, status FROM issue_reports WHERE id = ?', (issue_id,)).fetchone()
    if not row:
        return jsonify({"error": "Không tìm thấy báo cáo này."}), 404
    new_status = 'open' if row['status'] == 'resolved' else 'resolved'
    resolved_at = now_iso() if new_status == 'resolved' else None
    db.execute('UPDATE issue_reports SET status = ?, resolved_at = ? WHERE id = ?',
               (new_status, resolved_at, issue_id))
    db.commit()
    return jsonify({"success": True, "status": new_status})


@app.route('/developer/export.csv')
@developer_required
def developer_export_csv():
    import csv as csv_module
    db = get_db()
    rows = db.execute('''
        SELECT l.id, u.username AS username, l.endpoint, l.subject, l.mode,
               l.message_chars, l.response_chars, l.had_file, l.had_image, l.status, l.created_at
        FROM usage_logs l LEFT JOIN users u ON u.id = l.user_id
        ORDER BY l.id ASC
    ''').fetchall()

    buf = io.StringIO()
    writer = csv_module.writer(buf)
    writer.writerow(['id', 'username', 'endpoint', 'subject', 'mode', 'message_chars',
                      'response_chars', 'had_file', 'had_image', 'status', 'created_at'])
    for r in rows:
        writer.writerow([r['id'], r['username'] or '', r['endpoint'], r['subject'] or '',
                          r['mode'] or '', r['message_chars'], r['response_chars'],
                          r['had_file'], r['had_image'], r['status'], r['created_at']])

    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=usage_logs.csv'}
    )


# ==========================================
# 7. GỌI API AI (xAI / Consolex-compatible) — STREAMING
# ==========================================
def stream_consolex_ai(system_prompt: str, user_content):
    """Gọi xAI API ở chế độ stream=True và yield từng đoạn token nhận được.

    Dùng SESSION (requests.Session) để tái sử dụng kết nối TCP/TLS,
    giúp giảm độ trễ so với việc tạo kết nối mới mỗi lần gọi.
    """
    if not XAI_API_KEY:
        raise RuntimeError("Thiếu XAI_API_KEY. Vui lòng thiết lập biến môi trường trước khi chạy server.")

    url = f"{CONSOLEX_API_BASE.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {XAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": CONSOLEX_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.7,
        "max_tokens": 800,
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
def _truncate_text(full_text: str) -> str:
    truncated = full_text[:MAX_FILE_CHARS]
    if len(full_text) > MAX_FILE_CHARS:
        truncated += "\n\n[... nội dung bị cắt bớt do quá dài ...]"
    return truncated


def handle_pdf_upload(f):
    try:
        reader = PdfReader(f.stream)
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

        return jsonify({"text": _truncate_text(full_text), "pages": num_pages})
    except Exception as e:
        return jsonify({"error": f"Không đọc được file PDF: {e}"}), 500


def handle_docx_upload(f):
    if docx_lib is None:
        return jsonify({
            "error": "Server chưa cài thư viện đọc Word. Vui lòng chạy: pip install python-docx"
        }), 500
    try:
        document = docx_lib.Document(f.stream)
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

        return jsonify({"text": _truncate_text(full_text), "pages": None})
    except Exception as e:
        return jsonify({"error": f"Không đọc được file Word: {e}"}), 500


def handle_text_upload(f):
    try:
        raw = f.read()
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            text = raw.decode('utf-8', errors='ignore')
        text = text.strip()

        if not text:
            return jsonify({"error": "File này không có nội dung."}), 200

        return jsonify({"text": _truncate_text(text), "pages": None})
    except Exception as e:
        return jsonify({"error": f"Không đọc được file: {e}"}), 500


def handle_image_upload(f, filename, ext):
    raw = f.read()
    if len(raw) > MAX_IMAGE_BYTES:
        return jsonify({
            "error": f"Ảnh quá lớn (>{MAX_IMAGE_BYTES // (1024 * 1024)}MB). Em thử ảnh nhỏ hơn nhé!"
        }), 400

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


@app.route('/api/upload', methods=['POST'])
@login_required
def upload_file():
    if 'file' not in request.files:
        return jsonify({"error": "Không tìm thấy file trong yêu cầu."}), 400

    f = request.files['file']
    filename = f.filename or ''
    ext = os.path.splitext(filename.lower())[1]

    if ext in ALLOWED_IMAGE_EXT:
        return handle_image_upload(f, filename, ext)
    if ext == '.pdf':
        return handle_pdf_upload(f)
    if ext == '.docx':
        return handle_docx_upload(f)
    if ext in ('.txt', '.csv'):
        return handle_text_upload(f)

    return jsonify({
        "error": f"Định dạng {ext or 'không xác định'} chưa được hỗ trợ. "
                 "Em thử PDF, Word (.docx), .txt, .csv hoặc ảnh (PNG/JPG/GIF/WEBP) nhé!"
    }), 400


# ==========================================
# 9. LỊCH SỬ HỘI THOẠI (theo tài khoản đăng nhập)
# ==========================================
@app.route('/api/conversations', methods=['GET'])
@login_required
def list_conversations():
    db = get_db()
    rows = db.execute(
        'SELECT id, title, updated_at, pinned, project_id FROM conversations WHERE user_id = ? '
        'ORDER BY pinned DESC, updated_at DESC',
        (current_user_id(),)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/conversations/<int:conv_id>', methods=['PATCH'])
@login_required
def update_conversation(conv_id):
    db = get_db()
    conv = db.execute(
        'SELECT id FROM conversations WHERE id = ? AND user_id = ?', (conv_id, current_user_id())
    ).fetchone()
    if not conv:
        return jsonify({"error": "Không tìm thấy đoạn chat này."}), 404

    data = request.get_json(silent=True) or {}
    fields, params = [], []

    if 'title' in data:
        title = (data.get('title') or '').strip()
        if not title:
            return jsonify({"error": "Tên đoạn chat không được để trống."}), 400
        fields.append('title = ?')
        params.append(title[:120])

    if 'pinned' in data:
        fields.append('pinned = ?')
        params.append(1 if data.get('pinned') else 0)

    if 'project_id' in data:
        project_id = data.get('project_id')
        if project_id is not None:
            proj = db.execute(
                'SELECT id FROM projects WHERE id = ? AND user_id = ?', (project_id, current_user_id())
            ).fetchone()
            if not proj:
                return jsonify({"error": "Không tìm thấy dự án này."}), 404
        fields.append('project_id = ?')
        params.append(project_id)

    if not fields:
        return jsonify({"error": "Không có gì để cập nhật."}), 400

    params.append(conv_id)
    db.execute(f"UPDATE conversations SET {', '.join(fields)} WHERE id = ?", params)
    db.commit()
    return jsonify({"success": True})


@app.route('/api/conversations/delete-all', methods=['POST'])
@login_required
def delete_all_conversations():
    db = get_db()
    conv_ids = [r['id'] for r in db.execute(
        'SELECT id FROM conversations WHERE user_id = ?', (current_user_id(),)
    ).fetchall()]
    if conv_ids:
        placeholders = ','.join('?' * len(conv_ids))
        db.execute(f'DELETE FROM messages WHERE conversation_id IN ({placeholders})', conv_ids)
        db.execute(f'DELETE FROM conversations WHERE id IN ({placeholders})', conv_ids)
        db.commit()
    return jsonify({"success": True, "deleted": len(conv_ids)})


# ---- Dự án (Projects) ----
@app.route('/api/projects', methods=['GET'])
@login_required
def list_projects():
    db = get_db()
    rows = db.execute(
        'SELECT id, name, created_at FROM projects WHERE user_id = ? ORDER BY created_at DESC',
        (current_user_id(),)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/projects', methods=['POST'])
@login_required
def create_project():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({"error": "Tên dự án không được để trống."}), 400
    db = get_db()
    cur = db.execute(
        'INSERT INTO projects (user_id, name, created_at) VALUES (?, ?, ?)',
        (current_user_id(), name[:80], now_iso())
    )
    db.commit()
    return jsonify({"id": cur.lastrowid, "name": name[:80]})


@app.route('/api/projects/<int:project_id>', methods=['DELETE'])
@login_required
def delete_project(project_id):
    db = get_db()
    proj = db.execute(
        'SELECT id FROM projects WHERE id = ? AND user_id = ?', (project_id, current_user_id())
    ).fetchone()
    if not proj:
        return jsonify({"error": "Không tìm thấy dự án này."}), 404
    db.execute('UPDATE conversations SET project_id = NULL WHERE project_id = ?', (project_id,))
    db.execute('DELETE FROM projects WHERE id = ?', (project_id,))
    db.commit()
    return jsonify({"success": True})


# ---- Tùy chọn cá nhân (Preferences) ----
@app.route('/api/preferences', methods=['GET'])
@login_required
def api_get_preferences():
    return jsonify(get_preferences(current_user_id()))


@app.route('/api/preferences', methods=['POST'])
@login_required
def api_set_preferences():
    data = request.get_json(silent=True) or {}
    return jsonify(set_preferences(current_user_id(), data))


# ---- Banner hệ thống (chỉ đọc, cho mọi người dùng đã đăng nhập) ----
@app.route('/api/banner', methods=['GET'])
@login_required
def api_get_banner():
    return jsonify(get_banner())


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


# ==========================================
# 10. CHAT (STREAMING QUA SERVER-SENT EVENTS) + LƯU LỊCH SỬ
# ==========================================
@app.route('/api/report-issue', methods=['POST'])
@login_required
def report_issue():
    """Học sinh báo lỗi 1 câu trả lời cụ thể (hoặc báo lỗi chung). Lưu lại để developer xem
    và xử lý ở trang /developer."""
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


@app.route('/api/memories', methods=['GET'])
@login_required
def api_list_memories():
    db = get_db()
    rows = db.execute(
        'SELECT id, content, source, created_at FROM memories WHERE user_id = ? ORDER BY created_at DESC',
        (current_user_id(),)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/memories', methods=['DELETE'])
@login_required
def api_clear_memories():
    """Xoá toàn bộ 'bộ nhớ' AI của chính học sinh này (quyền riêng tư — mỗi người chỉ xoá
    được bộ nhớ của mình)."""
    db = get_db()
    db.execute('DELETE FROM memories WHERE user_id = ?', (current_user_id(),))
    db.commit()
    return jsonify({"success": True})


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

    if not user_message:
        return jsonify({"error": "Em chưa nhập câu hỏi nào cả."}), 400

    # Input validation cơ bản để tránh payload bất thường.
    if len(user_message) > 4000:
        return jsonify({"error": "Câu hỏi quá dài, em rút gọn lại giúp Thầy/Cô nhé!"}), 400
    if image_data and not image_data.startswith('data:image/'):
        return jsonify({"error": "Dữ liệu ảnh không hợp lệ."}), 400

    user_id = current_user_id()
    db = get_db()

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

    # "Bộ nhớ" AI: phát hiện + lưu 1 mục mới từ tin nhắn này (nếu có), rồi lấy những gì
    # đã ghi nhớ trước đó để cá nhân hoá câu trả lời.
    new_memory = extract_and_save_memory(user_id, user_message)
    recent_memories = get_recent_memories(user_id)

    system_prompt = f"""
    Bạn là StudyMate AI Pro, gia sư THCS (lớp 6-9) tận tâm.
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

    if recent_memories:
        mem_lines = "\n".join(f"    - {m}" for m in recent_memories)
        system_prompt += f"""

    Những điều Thầy/Cô đã ghi nhớ về học sinh này từ các lần trò chuyện trước:
{mem_lines}
    Hãy tận dụng thông tin này để cá nhân hoá câu trả lời khi phù hợp (vd: đúng trình độ
    lớp học), nhưng đừng nhắc lại y nguyên nếu không cần thiết.
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
        yield f"data: {json.dumps({'conversationId': conv_id})}\n\n"
        if new_memory:
            yield f"data: {json.dumps({'memory': new_memory})}\n\n"
        collected = []
        try:
            for token in stream_consolex_ai(system_prompt, user_content):
                collected.append(token)
                yield f"data: {json.dumps({'token': token})}\n\n"

            assistant_text = ''.join(collected).strip()
            if assistant_text:
                db.execute(
                    'INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)',
                    (conv_id, 'assistant', assistant_text, now_iso())
                )
                db.execute('UPDATE conversations SET updated_at = ? WHERE id = ?', (now_iso(), conv_id))
                db.commit()

            log_usage(user_id, subject, mode, len(user_message), len(assistant_text),
                      bool(file_context), bool(image_data), 'ok' if assistant_text else 'empty')
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
    print("🚀 StudyMate AI Pro đang chạy... Truy cập: http://localhost:5000")
    print("👤 Trang đăng nhập: http://localhost:5000/login")
    print(f"🔑 Đăng nhập Google: {'BẬT' if GOOGLE_OAUTH_ENABLED else 'tắt (chưa cấu hình .env)'}")
    print("🛡️ Để xem bảng báo cáo bảo mật... Truy cập: http://localhost:5000/security")
    # debug=True chỉ dùng khi phát triển trên máy cá nhân — KHÔNG bật khi deploy thật
    # (xem README phần "Deploy lên production" để chạy bằng gunicorn thay vì app.run).
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)

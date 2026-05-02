"""
app.py  ─  온라인 정보 공유 마당  ·  Flask 메인 애플리케이션
"""

import os
import sys
import json
import hashlib
import hmac
import secrets
import re
import datetime
import sqlite3
import time
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                   session, jsonify, flash, g, abort)
from flask_socketio import SocketIO, emit, join_room, leave_room

# C++ 엔진 래퍼
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "modules"))
from engine_wrapper import filter_and_sort, top_n_posts, engine_version

# ─────────────────────────────────────────────────────────────────
# 앱 초기화
# ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["SECRET_KEY"]             = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["DATABASE"]               = os.path.join(app.instance_path, "infoshare.db")
app.config["SESSION_COOKIE_HTTPONLY"]= True
app.config["SESSION_COOKIE_SAMESITE"]= "Lax"
app.config["MAX_CONTENT_LENGTH"]     = 5 * 1024 * 1024   # 5 MB 업로드 제한
app.config["PERMANENT_SESSION_LIFETIME"] = datetime.timedelta(hours=12)

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

os.makedirs(app.instance_path, exist_ok=True)

# ─────────────────────────────────────────────────────────────────
# 보안 상수
# ─────────────────────────────────────────────────────────────────
HASH_ALGORITHM    = "sha256"
HASH_ITERATIONS   = 260_000          # PBKDF2 반복 횟수 (NIST 2023 권고)
SALT_LENGTH       = 32               # bytes
MAX_LOGIN_FAILS   = 5                # 계정 잠금 임계값
LOCKOUT_SECONDS   = 900              # 15분

CATEGORIES = ["공지", "자유", "질문/답변", "수학토론", "공동연구", "문제공유", "기타"]

# ─────────────────────────────────────────────────────────────────
# 데이터베이스 헬퍼
# ─────────────────────────────────────────────────────────────────

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(
            app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query_db(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rv = cur.fetchall()
    return (rv[0] if rv else None) if one else rv


def exec_db(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid


# ─────────────────────────────────────────────────────────────────
# DB 스키마 초기화
# ─────────────────────────────────────────────────────────────────

def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT    NOT NULL UNIQUE,
            email       TEXT    NOT NULL UNIQUE,
            password_hash TEXT  NOT NULL,
            salt        TEXT    NOT NULL,
            avatar      TEXT    DEFAULT 'default.png',
            bio         TEXT    DEFAULT '',
            role        TEXT    DEFAULT 'user',
            is_active   INTEGER DEFAULT 1,
            fail_count  INTEGER DEFAULT 0,
            locked_until INTEGER DEFAULT 0,
            created_at  INTEGER NOT NULL,
            last_login  INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS login_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            ip          TEXT    NOT NULL,
            user_agent  TEXT    NOT NULL,
            success     INTEGER NOT NULL,
            reason      TEXT    DEFAULT '',
            created_at  INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS posts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            category    TEXT    NOT NULL,
            title       TEXT    NOT NULL,
            content     TEXT    NOT NULL,
            views       INTEGER DEFAULT 0,
            likes       INTEGER DEFAULT 0,
            is_deleted  INTEGER DEFAULT 0,
            created_at  INTEGER NOT NULL,
            updated_at  INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS post_likes (
            user_id INTEGER NOT NULL,
            post_id INTEGER NOT NULL,
            PRIMARY KEY (user_id, post_id)
        );

        CREATE TABLE IF NOT EXISTS comments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id     INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            parent_id   INTEGER DEFAULT NULL,
            content     TEXT    NOT NULL,
            likes       INTEGER DEFAULT 0,
            is_deleted  INTEGER DEFAULT 0,
            created_at  INTEGER NOT NULL,
            FOREIGN KEY (post_id) REFERENCES posts(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS chat_rooms (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            type        TEXT    DEFAULT 'group',   -- 'group' or 'dm'
            created_by  INTEGER NOT NULL,
            created_at  INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chat_members (
            room_id  INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            joined_at INTEGER NOT NULL,
            PRIMARY KEY (room_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS chat_messages (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id   INTEGER NOT NULL,
            user_id   INTEGER NOT NULL,
            content   TEXT    NOT NULL,
            msg_type  TEXT    DEFAULT 'text',
            created_at INTEGER NOT NULL,
            FOREIGN KEY (room_id)  REFERENCES chat_rooms(id),
            FOREIGN KEY (user_id)  REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            type        TEXT    NOT NULL,
            message     TEXT    NOT NULL,
            link        TEXT    DEFAULT '',
            is_read     INTEGER DEFAULT 0,
            created_at  INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_posts_category  ON posts(category);
        CREATE INDEX IF NOT EXISTS idx_posts_user      ON posts(user_id);
        CREATE INDEX IF NOT EXISTS idx_comments_post   ON comments(post_id);
        CREATE INDEX IF NOT EXISTS idx_chat_messages   ON chat_messages(room_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_login_logs_user ON login_logs(user_id);
    """)
    db.commit()

    # 기본 채팅방 생성
    existing = query_db("SELECT id FROM chat_rooms WHERE name='일반 채팅'", one=True)
    if not existing:
        db.execute(
            "INSERT INTO chat_rooms (name, type, created_by, created_at) VALUES (?,?,?,?)",
            ("일반 채팅", "group", 0, int(time.time()))
        )
        db.execute(
            "INSERT INTO chat_rooms (name, type, created_by, created_at) VALUES (?,?,?,?)",
            ("기술 토론", "group", 0, int(time.time()))
        )
        db.commit()


# ─────────────────────────────────────────────────────────────────
# 보안 유틸
# ─────────────────────────────────────────────────────────────────

def hash_password(password: str, salt: str = None):
    """PBKDF2-HMAC-SHA256 비밀번호 해싱"""
    if salt is None:
        salt = secrets.token_hex(SALT_LENGTH)
    key = hashlib.pbkdf2_hmac(
        HASH_ALGORITHM,
        password.encode("utf-8"),
        salt.encode("utf-8"),
        HASH_ITERATIONS,
    )
    return key.hex(), salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    key = hashlib.pbkdf2_hmac(
        HASH_ALGORITHM,
        password.encode("utf-8"),
        salt.encode("utf-8"),
        HASH_ITERATIONS,
    )
    return hmac.compare_digest(key.hex(), stored_hash)


def validate_password_strength(pw: str) -> str | None:
    """비밀번호 강도 검사. 오류 메시지 반환, 통과 시 None"""
    if len(pw) < 8:
        return "비밀번호는 8자 이상이어야 합니다."
    if not re.search(r"[A-Z]", pw):
        return "대문자를 1개 이상 포함해야 합니다."
    if not re.search(r"[a-z]", pw):
        return "소문자를 1개 이상 포함해야 합니다."
    if not re.search(r"\d", pw):
        return "숫자를 1개 이상 포함해야 합니다."
    if not re.search(r"[!@#$%^&*()_+\-=\[\]{};':\"\\|,.<>\/?]", pw):
        return "특수문자를 1개 이상 포함해야 합니다."
    return None


def record_login_log(user_id, success, reason=""):
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")
    ip = ip.split(",")[0].strip()
    ua = request.headers.get("User-Agent", "unknown")[:512]
    exec_db(
        "INSERT INTO login_logs (user_id, ip, user_agent, success, reason, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (user_id, ip, ua, 1 if success else 0, reason, int(time.time()))
    )


def get_client_ip():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    return ip.split(",")[0].strip()


# ─────────────────────────────────────────────────────────────────
# 데코레이터
# ─────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("로그인이 필요합니다.", "warning")
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            abort(401)
        user = query_db("SELECT role FROM users WHERE id=?", (session["user_id"],), one=True)
        if not user or user["role"] != "admin":
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────────────────────────────
# 컨텍스트 프로세서
# ─────────────────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    user = None
    notif_count = 0
    if "user_id" in session:
        user = query_db("SELECT * FROM users WHERE id=?", (session["user_id"],), one=True)
        notif_count = query_db(
            "SELECT COUNT(*) as c FROM notifications WHERE user_id=? AND is_read=0",
            (session["user_id"],), one=True
        )
        notif_count = notif_count["c"] if notif_count else 0
    return dict(
        current_user=user,
        notif_count=notif_count,
        categories=CATEGORIES,
        engine_ver=engine_version(),
        now=datetime.datetime.now(),
    )


# ─────────────────────────────────────────────────────────────────
# 라우트: 메인
# ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    # 최신 게시물 10개
    recent = query_db(
        "SELECT p.*, u.username FROM posts p "
        "JOIN users u ON p.user_id=u.id "
        "WHERE p.is_deleted=0 ORDER BY p.created_at DESC LIMIT 10"
    )
    # C++ 엔진으로 인기 게시물 계산
    all_posts_raw = query_db(
        "SELECT p.id, p.views, p.likes, p.created_at, "
        "COUNT(c.id) as comment_count "
        "FROM posts p LEFT JOIN comments c ON c.post_id=p.id AND c.is_deleted=0 "
        "WHERE p.is_deleted=0 GROUP BY p.id"
    )
    post_dicts = [dict(r) for r in all_posts_raw]
    top_ids    = top_n_posts(post_dicts, 5)
    popular    = []
    if top_ids:
        placeholders = ",".join("?" * len(top_ids))
        popular = query_db(
            f"SELECT p.*, u.username FROM posts p "
            f"JOIN users u ON p.user_id=u.id "
            f"WHERE p.id IN ({placeholders})",
            top_ids
        )
        popular = sorted(popular, key=lambda r: top_ids.index(r["id"]))

    # 카테고리별 통계
    cat_stats = {}
    for cat in CATEGORIES:
        row = query_db(
            "SELECT COUNT(*) as c FROM posts WHERE category=? AND is_deleted=0",
            (cat,), one=True
        )
        cat_stats[cat] = row["c"] if row else 0

    return render_template("index.html",
                           recent=recent,
                           popular=popular,
                           cat_stats=cat_stats)


# ─────────────────────────────────────────────────────────────────
# 라우트: 회원가입 / 로그인 / 로그아웃
# ─────────────────────────────────────────────────────────────────

@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email    = request.form.get("email", "").strip().lower()
        pw       = request.form.get("password", "")
        pw2      = request.form.get("password2", "")

        errors = []
        if not username or len(username) < 2 or len(username) > 20:
            errors.append("아이디는 2~20자여야 합니다.")
        if not re.match(r"^[a-zA-Z0-9_가-힣]+$", username):
            errors.append("아이디에 허용되지 않는 문자가 포함되어 있습니다.")
        if not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
            errors.append("유효한 이메일 주소를 입력하세요.")
        if pw != pw2:
            errors.append("비밀번호가 일치하지 않습니다.")
        pw_err = validate_password_strength(pw)
        if pw_err:
            errors.append(pw_err)
        if query_db("SELECT id FROM users WHERE username=?", (username,), one=True):
            errors.append("이미 사용 중인 아이디입니다.")
        if query_db("SELECT id FROM users WHERE email=?", (email,), one=True):
            errors.append("이미 사용 중인 이메일입니다.")

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("register.html")

        pw_hash, salt = hash_password(pw)
        exec_db(
            "INSERT INTO users (username, email, password_hash, salt, created_at) "
            "VALUES (?,?,?,?,?)",
            (username, email, pw_hash, salt, int(time.time()))
        )
        flash("회원가입이 완료되었습니다. 로그인해 주세요.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("index"))

    if request.method == "POST":
        login_id = request.form.get("login_id", "").strip()
        pw       = request.form.get("password", "")

        # 이메일 또는 아이디로 조회
        user = query_db(
            "SELECT * FROM users WHERE username=? OR email=?",
            (login_id, login_id.lower()), one=True
        )

        if not user:
            flash("아이디/이메일 또는 비밀번호가 올바르지 않습니다.", "danger")
            return render_template("login.html")

        # 계정 활성화 확인
        if not user["is_active"]:
            flash("정지된 계정입니다. 관리자에게 문의하세요.", "danger")
            return render_template("login.html")

        # 잠금 확인
        now_ts = int(time.time())
        if user["locked_until"] > now_ts:
            remaining = user["locked_until"] - now_ts
            flash(f"로그인 시도 횟수 초과로 {remaining//60}분 {remaining%60}초 후 재시도 가능합니다.", "danger")
            record_login_log(user["id"], False, "locked")
            return render_template("login.html")

        # 비밀번호 검증
        if not verify_password(pw, user["password_hash"], user["salt"]):
            new_fail = user["fail_count"] + 1
            if new_fail >= MAX_LOGIN_FAILS:
                exec_db(
                    "UPDATE users SET fail_count=?, locked_until=? WHERE id=?",
                    (new_fail, now_ts + LOCKOUT_SECONDS, user["id"])
                )
                record_login_log(user["id"], False, "too_many_fails")
                flash(f"비밀번호 {MAX_LOGIN_FAILS}회 오류. 계정이 {LOCKOUT_SECONDS//60}분 잠금되었습니다.", "danger")
            else:
                exec_db("UPDATE users SET fail_count=? WHERE id=?", (new_fail, user["id"]))
                record_login_log(user["id"], False, "wrong_password")
                flash(f"비밀번호가 틀렸습니다. ({new_fail}/{MAX_LOGIN_FAILS}회)", "danger")
            return render_template("login.html")

        # 로그인 성공
        exec_db(
            "UPDATE users SET fail_count=0, locked_until=0, last_login=? WHERE id=?",
            (now_ts, user["id"])
        )
        record_login_log(user["id"], True)

        session.permanent = True
        session["user_id"]   = user["id"]
        session["username"]  = user["username"]
        session["user_role"] = user["role"]

        flash(f"어서오세요, {user['username']}님! 로그인되었습니다.", "success")
        next_url = request.args.get("next") or url_for("index")
        return redirect(next_url)

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    session.clear()
    flash("로그아웃되었습니다.", "info")
    return redirect(url_for("index"))


# ─────────────────────────────────────────────────────────────────
# 라우트: 게시판
# ─────────────────────────────────────────────────────────────────

@app.route("/board")
def board():
    category = request.args.get("category", "")
    keyword  = request.args.get("q", "")
    sort_by  = int(request.args.get("sort", 0))
    page     = max(1, int(request.args.get("page", 1)))
    per_page = 15
    offset   = (page - 1) * per_page

    # 모든 게시물 메타데이터를 C++ 엔진에 전달
    all_raw = query_db(
        "SELECT p.id, p.title, p.category, u.username as author, "
        "p.created_at, p.views, p.likes, "
        "COUNT(c.id) as comment_count "
        "FROM posts p "
        "JOIN users u ON p.user_id=u.id "
        "LEFT JOIN comments c ON c.post_id=p.id AND c.is_deleted=0 "
        "WHERE p.is_deleted=0 "
        "GROUP BY p.id"
    )
    post_dicts = [dict(r) for r in all_raw]

    sorted_ids, total = filter_and_sort(
        post_dicts,
        category=category,
        keyword=keyword,
        sort_by=sort_by,
        order=0,
        limit=per_page,
        offset=offset,
    )

    posts = []
    if sorted_ids:
        placeholders = ",".join("?" * len(sorted_ids))
        rows = query_db(
            f"SELECT p.*, u.username, "
            f"(SELECT COUNT(*) FROM comments c WHERE c.post_id=p.id AND c.is_deleted=0) as cmt_cnt "
            f"FROM posts p JOIN users u ON p.user_id=u.id "
            f"WHERE p.id IN ({placeholders})",
            sorted_ids
        )
        id_map = {r["id"]: r for r in rows}
        posts  = [id_map[i] for i in sorted_ids if i in id_map]

    total_pages = max(1, (total + per_page - 1) // per_page)

    return render_template("board.html",
                           posts=posts,
                           category=category,
                           keyword=keyword,
                           sort_by=sort_by,
                           page=page,
                           total_pages=total_pages,
                           total=total)


@app.route("/post/new", methods=["GET", "POST"])
@login_required
def post_new():
    if request.method == "POST":
        title    = request.form.get("title", "").strip()
        content  = request.form.get("content", "").strip()
        category = request.form.get("category", "기타")

        if not title or len(title) < 2:
            flash("제목을 2자 이상 입력하세요.", "danger")
            return render_template("post_form.html", mode="new")
        if not content or len(content) < 5:
            flash("내용을 5자 이상 입력하세요.", "danger")
            return render_template("post_form.html", mode="new")
        if category not in CATEGORIES:
            category = "기타"

        now_ts = int(time.time())
        pid = exec_db(
            "INSERT INTO posts (user_id, category, title, content, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (session["user_id"], category, title, content, now_ts, now_ts)
        )
        flash("게시물이 등록되었습니다.", "success")
        return redirect(url_for("post_view", pid=pid))

    return render_template("post_form.html", mode="new")


@app.route("/post/<int:pid>")
def post_view(pid):
    post = query_db(
        "SELECT p.*, u.username, u.avatar FROM posts p "
        "JOIN users u ON p.user_id=u.id WHERE p.id=? AND p.is_deleted=0",
        (pid,), one=True
    )
    if not post:
        abort(404)

    # 조회수 증가 (세션 중복 방지)
    viewed_key = f"viewed_{pid}"
    if viewed_key not in session:
        exec_db("UPDATE posts SET views=views+1 WHERE id=?", (pid,))
        session[viewed_key] = True

    # 댓글 (계층 구조)
    comments = query_db(
        "SELECT c.*, u.username, u.avatar FROM comments c "
        "JOIN users u ON c.user_id=u.id "
        "WHERE c.post_id=? ORDER BY c.created_at ASC",
        (pid,)
    )

    # 좋아요 여부
    liked = False
    if "user_id" in session:
        row = query_db(
            "SELECT 1 FROM post_likes WHERE user_id=? AND post_id=?",
            (session["user_id"], pid), one=True
        )
        liked = row is not None

    return render_template("post_view.html", post=post, comments=comments, liked=liked)


@app.route("/post/<int:pid>/edit", methods=["GET", "POST"])
@login_required
def post_edit(pid):
    post = query_db(
        "SELECT * FROM posts WHERE id=? AND is_deleted=0", (pid,), one=True
    )
    if not post:
        abort(404)
    if post["user_id"] != session["user_id"] and session.get("user_role") != "admin":
        abort(403)

    if request.method == "POST":
        title    = request.form.get("title", "").strip()
        content  = request.form.get("content", "").strip()
        category = request.form.get("category", post["category"])
        if category not in CATEGORIES:
            category = post["category"]

        if not title or len(title) < 2:
            flash("제목을 2자 이상 입력하세요.", "danger")
            return render_template("post_form.html", mode="edit", post=post)

        exec_db(
            "UPDATE posts SET title=?, content=?, category=?, updated_at=? WHERE id=?",
            (title, content, category, int(time.time()), pid)
        )
        flash("게시물이 수정되었습니다.", "success")
        return redirect(url_for("post_view", pid=pid))

    return render_template("post_form.html", mode="edit", post=post)


@app.route("/post/<int:pid>/delete", methods=["POST"])
@login_required
def post_delete(pid):
    post = query_db("SELECT * FROM posts WHERE id=?", (pid,), one=True)
    if not post:
        abort(404)
    if post["user_id"] != session["user_id"] and session.get("user_role") != "admin":
        abort(403)
    exec_db("UPDATE posts SET is_deleted=1 WHERE id=?", (pid,))
    flash("게시물이 삭제되었습니다.", "info")
    return redirect(url_for("board"))


@app.route("/post/<int:pid>/like", methods=["POST"])
@login_required
def post_like(pid):
    uid = session["user_id"]
    existing = query_db(
        "SELECT 1 FROM post_likes WHERE user_id=? AND post_id=?", (uid, pid), one=True
    )
    if existing:
        exec_db("DELETE FROM post_likes WHERE user_id=? AND post_id=?", (uid, pid))
        exec_db("UPDATE posts SET likes=MAX(0, likes-1) WHERE id=?", (pid,))
        liked = False
    else:
        exec_db("INSERT OR IGNORE INTO post_likes (user_id, post_id) VALUES (?,?)", (uid, pid))
        exec_db("UPDATE posts SET likes=likes+1 WHERE id=?", (pid,))
        liked = True
        # 작성자에게 알림
        post = query_db("SELECT user_id FROM posts WHERE id=?", (pid,), one=True)
        if post and post["user_id"] != uid:
            add_notification(
                post["user_id"], "like",
                f"{session['username']}님이 게시물에 좋아요를 눌렀습니다.",
                url_for("post_view", pid=pid)
            )

    row = query_db("SELECT likes FROM posts WHERE id=?", (pid,), one=True)
    return jsonify({"liked": liked, "likes": row["likes"]})


# ─────────────────────────────────────────────────────────────────
# 라우트: 댓글
# ─────────────────────────────────────────────────────────────────

@app.route("/post/<int:pid>/comment", methods=["POST"])
@login_required
def comment_add(pid):
    post = query_db("SELECT * FROM posts WHERE id=? AND is_deleted=0", (pid,), one=True)
    if not post:
        abort(404)
    content   = request.form.get("content", "").strip()
    parent_id = request.form.get("parent_id", None)
    if not content:
        flash("댓글 내용을 입력하세요.", "danger")
        return redirect(url_for("post_view", pid=pid))

    parent_id = int(parent_id) if parent_id and str(parent_id).isdigit() else None
    exec_db(
        "INSERT INTO comments (post_id, user_id, parent_id, content, created_at) "
        "VALUES (?,?,?,?,?)",
        (pid, session["user_id"], parent_id, content, int(time.time()))
    )
    # 게시물 작성자에게 알림
    if post["user_id"] != session["user_id"]:
        add_notification(
            post["user_id"], "comment",
            f"{session['username']}님이 게시물에 댓글을 달았습니다.",
            url_for("post_view", pid=pid)
        )
    flash("댓글이 등록되었습니다.", "success")
    return redirect(url_for("post_view", pid=pid))


@app.route("/comment/<int:cid>/delete", methods=["POST"])
@login_required
def comment_delete(cid):
    cmt = query_db("SELECT * FROM comments WHERE id=?", (cid,), one=True)
    if not cmt:
        abort(404)
    if cmt["user_id"] != session["user_id"] and session.get("user_role") != "admin":
        abort(403)
    exec_db("UPDATE comments SET is_deleted=1 WHERE id=?", (cid,))
    return redirect(url_for("post_view", pid=cmt["post_id"]))


# ─────────────────────────────────────────────────────────────────
# 라우트: 채팅
# ─────────────────────────────────────────────────────────────────

@app.route("/chat")
@login_required
def chat():
    # 참여 가능한 그룹 채팅방 목록
    rooms = query_db(
        "SELECT r.*, COUNT(m.id) as msg_count "
        "FROM chat_rooms r "
        "LEFT JOIN chat_messages m ON m.room_id=r.id "
        "WHERE r.type='group' "
        "GROUP BY r.id ORDER BY r.created_at ASC"
    )
    # DM 목록
    dm_rooms = query_db(
        "SELECT r.*, u.username as other_user "
        "FROM chat_rooms r "
        "JOIN chat_members cm ON cm.room_id=r.id AND cm.user_id=? "
        "JOIN chat_members cm2 ON cm2.room_id=r.id AND cm2.user_id!=? "
        "JOIN users u ON u.id=cm2.user_id "
        "WHERE r.type='dm'",
        (session["user_id"], session["user_id"])
    )
    return render_template("chat.html", rooms=rooms, dm_rooms=dm_rooms)


@app.route("/chat/room/<int:room_id>")
@login_required
def chat_room(room_id):
    room = query_db("SELECT * FROM chat_rooms WHERE id=?", (room_id,), one=True)
    if not room:
        abort(404)

    # 그룹 채팅은 모두 접근 가능, DM은 멤버만
    if room["type"] == "dm":
        member = query_db(
            "SELECT 1 FROM chat_members WHERE room_id=? AND user_id=?",
            (room_id, session["user_id"]), one=True
        )
        if not member:
            abort(403)

    # 최근 메시지 50개
    messages = query_db(
        "SELECT m.*, u.username, u.avatar FROM chat_messages m "
        "JOIN users u ON u.id=m.user_id "
        "WHERE m.room_id=? ORDER BY m.created_at DESC LIMIT 50",
        (room_id,)
    )
    messages = list(reversed(messages))

    # 멤버 목록
    members = query_db(
        "SELECT u.id, u.username, u.avatar FROM chat_members cm "
        "JOIN users u ON u.id=cm.user_id WHERE cm.room_id=?",
        (room_id,)
    )

    return render_template("chat_room.html", room=room, messages=messages, members=members)


@app.route("/chat/dm/<int:target_id>", methods=["POST"])
@login_required
def start_dm(target_id):
    my_id = session["user_id"]
    if target_id == my_id:
        flash("자신에게 DM을 보낼 수 없습니다.", "warning")
        return redirect(url_for("chat"))

    target = query_db("SELECT * FROM users WHERE id=?", (target_id,), one=True)
    if not target:
        abort(404)

    # 기존 DM 방 조회
    existing = query_db(
        "SELECT r.id FROM chat_rooms r "
        "JOIN chat_members m1 ON m1.room_id=r.id AND m1.user_id=? "
        "JOIN chat_members m2 ON m2.room_id=r.id AND m2.user_id=? "
        "WHERE r.type='dm' LIMIT 1",
        (my_id, target_id), one=True
    )
    if existing:
        return redirect(url_for("chat_room", room_id=existing["id"]))

    # 새 DM 방 생성
    now_ts = int(time.time())
    room_id = exec_db(
        "INSERT INTO chat_rooms (name, type, created_by, created_at) VALUES (?,?,?,?)",
        (f"DM:{my_id}-{target_id}", "dm", my_id, now_ts)
    )
    exec_db("INSERT INTO chat_members (room_id, user_id, joined_at) VALUES (?,?,?)", (room_id, my_id, now_ts))
    exec_db("INSERT INTO chat_members (room_id, user_id, joined_at) VALUES (?,?,?)", (room_id, target_id, now_ts))
    return redirect(url_for("chat_room", room_id=room_id))


# ─────────────────────────────────────────────────────────────────
# 라우트: 사용자 프로필 / 로그인 내역
# ─────────────────────────────────────────────────────────────────

@app.route("/profile/<username>")
def profile(username):
    user = query_db("SELECT * FROM users WHERE username=?", (username,), one=True)
    if not user:
        abort(404)
    posts = query_db(
        "SELECT * FROM posts WHERE user_id=? AND is_deleted=0 ORDER BY created_at DESC LIMIT 10",
        (user["id"],)
    )
    return render_template("profile.html", profile_user=user, posts=posts)


@app.route("/settings/login-history")
@login_required
def login_history():
    logs = query_db(
        "SELECT * FROM login_logs WHERE user_id=? ORDER BY created_at DESC LIMIT 50",
        (session["user_id"],)
    )
    return render_template("login_history.html", logs=logs)


@app.route("/settings/profile", methods=["GET", "POST"])
@login_required
def settings_profile():
    user = query_db("SELECT * FROM users WHERE id=?", (session["user_id"],), one=True)
    if request.method == "POST":
        bio      = request.form.get("bio", "").strip()[:300]
        new_pw   = request.form.get("new_password", "")
        curr_pw  = request.form.get("current_password", "")

        if new_pw:
            if not verify_password(curr_pw, user["password_hash"], user["salt"]):
                flash("현재 비밀번호가 올바르지 않습니다.", "danger")
                return render_template("settings_profile.html", user=user)
            pw_err = validate_password_strength(new_pw)
            if pw_err:
                flash(pw_err, "danger")
                return render_template("settings_profile.html", user=user)
            new_hash, new_salt = hash_password(new_pw)
            exec_db(
                "UPDATE users SET password_hash=?, salt=?, bio=? WHERE id=?",
                (new_hash, new_salt, bio, session["user_id"])
            )
            flash("비밀번호와 프로필이 업데이트되었습니다.", "success")
        else:
            exec_db("UPDATE users SET bio=? WHERE id=?", (bio, session["user_id"]))
            flash("프로필이 업데이트되었습니다.", "success")

        return redirect(url_for("settings_profile"))

    return render_template("settings_profile.html", user=user)


# ─────────────────────────────────────────────────────────────────
# 라우트: 알림
# ─────────────────────────────────────────────────────────────────

def add_notification(user_id, ntype, message, link=""):
    exec_db(
        "INSERT INTO notifications (user_id, type, message, link, created_at) "
        "VALUES (?,?,?,?,?)",
        (user_id, ntype, message, link, int(time.time()))
    )


@app.route("/notifications")
@login_required
def notifications():
    notifs = query_db(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 50",
        (session["user_id"],)
    )
    exec_db(
        "UPDATE notifications SET is_read=1 WHERE user_id=? AND is_read=0",
        (session["user_id"],)
    )
    return render_template("notifications.html", notifs=notifs)


@app.route("/api/notifications/unread")
@login_required
def api_notif_count():
    row = query_db(
        "SELECT COUNT(*) as c FROM notifications WHERE user_id=? AND is_read=0",
        (session["user_id"],), one=True
    )
    return jsonify({"count": row["c"] if row else 0})


# ─────────────────────────────────────────────────────────────────
# SocketIO: 실시간 채팅
# ─────────────────────────────────────────────────────────────────

@socketio.on("join")
def on_join(data):
    if "user_id" not in session:
        return
    room_id = data.get("room_id")
    join_room(str(room_id))
    emit("status", {
        "message": f"{session['username']}님이 입장했습니다.",
        "username": session["username"],
        "type": "join",
    }, room=str(room_id))


@socketio.on("leave")
def on_leave(data):
    if "user_id" not in session:
        return
    room_id = data.get("room_id")
    leave_room(str(room_id))
    emit("status", {
        "message": f"{session['username']}님이 퇴장했습니다.",
        "username": session["username"],
        "type": "leave",
    }, room=str(room_id))


@socketio.on("message")
def on_message(data):
    if "user_id" not in session:
        return
    room_id = int(data.get("room_id", 0))
    content = str(data.get("content", "")).strip()[:2000]
    if not content:
        return

    room = query_db("SELECT * FROM chat_rooms WHERE id=?", (room_id,), one=True)
    if not room:
        return

    # DM 권한 확인
    if room["type"] == "dm":
        member = query_db(
            "SELECT 1 FROM chat_members WHERE room_id=? AND user_id=?",
            (room_id, session["user_id"]), one=True
        )
        if not member:
            return

    now_ts = int(time.time())
    msg_id = exec_db(
        "INSERT INTO chat_messages (room_id, user_id, content, created_at) VALUES (?,?,?,?)",
        (room_id, session["user_id"], content, now_ts)
    )

    emit("message", {
        "id":        msg_id,
        "username":  session["username"],
        "user_id":   session["user_id"],
        "content":   content,
        "created_at": now_ts,
        "room_id":   room_id,
    }, room=str(room_id))


# ─────────────────────────────────────────────────────────────────
# 관리자
# ─────────────────────────────────────────────────────────────────

@app.route("/admin")
@admin_required
def admin_panel():
    user_count = query_db("SELECT COUNT(*) as c FROM users", one=True)["c"]
    post_count = query_db("SELECT COUNT(*) as c FROM posts WHERE is_deleted=0", one=True)["c"]
    today_logins = query_db(
        "SELECT COUNT(*) as c FROM login_logs WHERE success=1 AND created_at > ?",
        (int(time.time()) - 86400,), one=True
    )["c"]
    return render_template("admin.html",
                           user_count=user_count,
                           post_count=post_count,
                           today_logins=today_logins)


@app.route("/admin/users")
@admin_required
def admin_users():
    users = query_db("SELECT * FROM users ORDER BY created_at DESC")
    return render_template("admin_users.html", users=users)


@app.route("/admin/users/<int:uid>/toggle", methods=["POST"])
@admin_required
def admin_toggle_user(uid):
    user = query_db("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if user:
        new_status = 0 if user["is_active"] else 1
        exec_db("UPDATE users SET is_active=? WHERE id=?", (new_status, uid))
        status_text = "활성화" if new_status else "정지"
        flash(f"사용자 {user['username']} 계정이 {status_text}되었습니다.", "success")
    return redirect(url_for("admin_users"))


# ─────────────────────────────────────────────────────────────────
# 에러 핸들러
# ─────────────────────────────────────────────────────────────────

@app.route("/favicon.ico")
def favicon():
    return "", 204   # No Content — 브라우저 요청 조용히 무시


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="페이지를 찾을 수 없습니다."), 404

@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403, message="접근 권한이 없습니다."), 403

@app.errorhandler(500)
def server_error(e):
    return render_template("error.html", code=500, message="서버 오류가 발생했습니다."), 500


# ─────────────────────────────────────────────────────────────────
# 템플릿 필터
# ─────────────────────────────────────────────────────────────────

@app.template_filter("ts_to_dt")
def ts_to_dt(ts):
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "-"

@app.template_filter("ts_to_date")
def ts_to_date(ts):
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except Exception:
        return "-"

@app.template_filter("time_ago")
def time_ago(ts):
    try:
        diff = int(time.time()) - int(ts)
        if diff < 60:    return "방금 전"
        if diff < 3600:  return f"{diff//60}분 전"
        if diff < 86400: return f"{diff//3600}시간 전"
        if diff < 2592000: return f"{diff//86400}일 전"
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except Exception:
        return "-"


# ─────────────────────────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    with app.app_context():
        init_db()
        print(f"[INFO] DB initialized: {app.config['DATABASE']}")
        print(f"[INFO] Engine: {engine_version()}")
    socketio.run(app, host="0.0.0.0", port=5000, debug=True, allow_unsafe_werkzeug=True)
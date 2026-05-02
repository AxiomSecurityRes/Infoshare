# 정보 공유 마당 — 배포 및 외부 접속 가이드

## 프로젝트 파일 구조

```
infoshare/
├── app.py                        # Flask 메인 애플리케이션 (백엔드 전체)
├── requirements.txt              # Python 패키지 목록
├── modules/
│   ├── data_engine.cpp           # C++ 고성능 정렬/필터 엔진
│   ├── data_engine.so            # 컴파일된 공유 라이브러리 (리눅스)
│   └── engine_wrapper.py         # Python ↔ C++ ctypes 래퍼
├── static/
│   ├── css/main.css              # 전체 스타일시트
│   └── js/main.js                # 프런트엔드 스크립트
├── templates/
│   ├── base.html                 # 공통 레이아웃 (네비, 푸터)
│   ├── index.html                # 메인 페이지
│   ├── board.html                # 게시판 목록
│   ├── post_view.html            # 게시물 상세 + 댓글
│   ├── post_form.html            # 글쓰기/수정 폼
│   ├── login.html                # 로그인
│   ├── register.html             # 회원가입
│   ├── chat.html                 # 채팅 로비
│   ├── chat_room.html            # 채팅방 (실시간 SocketIO)
│   ├── profile.html              # 사용자 프로필
│   ├── settings_profile.html     # 프로필 설정
│   ├── login_history.html        # 로그인 내역
│   ├── notifications.html        # 알림
│   ├── error.html                # 오류 페이지
│   ├── admin.html                # 관리자 대시보드
│   └── admin_users.html          # 회원 관리
└── instance/
    └── infoshare.db              # SQLite DB (자동 생성)
```

---

## 1단계: 로컬 실행

### 사전 준비
```bash
# Python 3.10 이상 확인
python --version

# g++ 컴파일러 확인 (없으면 설치)
# Ubuntu/Debian:
sudo apt install g++ -y
# macOS:
xcode-select --install
```

### 설치 및 실행
```bash
# 1) 프로젝트 폴더로 이동
cd infoshare

# 2) 가상환경 생성 (권장)
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 3) Python 패키지 설치
pip install -r requirements.txt

# 4) C++ 엔진 컴파일
cd modules
g++ -O2 -shared -fPIC -o data_engine.so data_engine.cpp
cd ..

# 5) 앱 실행
python app.py
```

브라우저에서 `http://127.0.0.1:5000` 접속

**기본 관리자 계정**
- 아이디: `admin`
- 비밀번호: `Admin1234!`
- ⚠️ 반드시 로그인 후 비밀번호를 변경하세요.

---

## 2단계: 외부 접속 방법

### 방법 A — Ngrok (가장 간단, 즉시 외부 접속)

도메인 구매 없이 5분 안에 외부에서 접속 가능한 방법입니다.

```bash
# 1) ngrok 설치
# Linux:
curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc \
  | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null \
  && echo "deb https://ngrok-agent.s3.amazonaws.com buster main" \
  | sudo tee /etc/apt/sources.list.d/ngrok.list \
  && sudo apt update && sudo apt install ngrok

# macOS (Homebrew):
brew install ngrok/ngrok/ngrok

# Windows: https://ngrok.com/download 에서 다운로드

# 2) https://ngrok.com 에서 무료 계정 가입 후 인증 토큰 설정
ngrok config add-authtoken <YOUR_TOKEN>

# 3) Flask 앱 실행 (별도 터미널)
python app.py

# 4) ngrok 터널 시작 (다른 터미널)
ngrok http 5000
```

실행 후 출력되는 `https://xxxx-xx-xx-xx-xx.ngrok-free.app` 주소로 외부 접속 가능.

> **주의**: 무료 플랜은 세션마다 URL이 바뀝니다. 고정 URL은 유료 플랜($8/월) 필요.

---

### 방법 B — Render.com (무료 상시 운영, 권장)

GitHub에 코드를 올리면 자동으로 배포되는 방식입니다.

#### B-1) 준비 파일 추가

**`Procfile`** (프로젝트 루트에 생성):
```
web: gunicorn --worker-class eventlet -w 1 app:app
```

**`build.sh`** (프로젝트 루트에 생성):
```bash
#!/bin/bash
pip install -r requirements.txt
cd modules && g++ -O2 -shared -fPIC -o data_engine.so data_engine.cpp && cd ..
```

**`requirements.txt`** 에 아래 추가:
```
gunicorn>=21.2.0
```

**`app.py`** 맨 아래 진입점 수정:
```python
if __name__ == "__main__":
    with app.app_context():
        init_db()
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
```

#### B-2) GitHub 연동 배포

```bash
# GitHub 저장소 생성 후
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/<YOUR_USERNAME>/infoshare.git
git push -u origin main
```

#### B-3) Render 설정

1. https://render.com 가입 (무료)
2. **New → Web Service** 클릭
3. GitHub 저장소 연결
4. 설정:
   - **Environment**: Python 3
   - **Build Command**: `chmod +x build.sh && ./build.sh`
   - **Start Command**: `gunicorn --worker-class eventlet -w 1 app:app`
5. **Environment Variables** 탭에서 추가:
   - `SECRET_KEY` = (랜덤 32자 문자열, 예: `openssl rand -hex 32` 결과값)
6. **Create Web Service** 클릭

약 2~3분 후 `https://infoshare-xxxx.onrender.com` 주소로 접속 가능.

> **무료 플랜 특징**: 15분 비활성 시 슬립 모드 진입 (첫 요청 30초 지연).
> 항시 활성화하려면 https://uptimerobot.com 에서 5분마다 핑 설정.

---

### 방법 C — PythonAnywhere (Python 전용 호스팅)

#### C-1) 가입 및 파일 업로드

1. https://www.pythonanywhere.com 무료 가입
2. **Files** 탭 → 파일 업로드 또는 Bash 콘솔에서:
```bash
git clone https://github.com/<YOUR_USERNAME>/infoshare.git
```

#### C-2) 가상환경 및 패키지 설치

**Bash 콘솔**에서:
```bash
cd infoshare
mkvirtualenv --python=/usr/bin/python3.10 infoshare-env
pip install flask flask-socketio eventlet gunicorn

# C++ 엔진 컴파일
cd modules
g++ -O2 -shared -fPIC -o data_engine.so data_engine.cpp
cd ..
```

#### C-3) WSGI 설정

1. **Web** 탭 → **Add a new web app**
2. **Manual configuration** → **Python 3.10** 선택
3. **WSGI configuration file** 클릭 후 내용을 아래로 교체:

```python
import sys
import os

project_home = '/home/<YOUR_USERNAME>/infoshare'
sys.path.insert(0, project_home)

os.environ['SECRET_KEY'] = 'your-secret-key-here'

from app import app, init_db, socketio
with app.app_context():
    init_db()

application = app
```

4. **Virtualenv** 항목에 가상환경 경로 입력:
   `/home/<YOUR_USERNAME>/.virtualenvs/infoshare-env`

5. **Static files** 설정:
   - URL: `/static/`
   - Directory: `/home/<YOUR_USERNAME>/infoshare/static`

6. **Reload** 버튼 클릭

> **주의**: PythonAnywhere 무료 플랜은 WebSocket을 지원하지 않아 실시간 채팅이 롱폴링 방식으로 작동합니다.
> WebSocket이 필요하면 Render 또는 Ngrok 방식을 사용하세요.

---

## 3단계: 운영 환경 보안 설정

### 환경 변수로 시크릿 관리

`.env` 파일 (절대 Git에 커밋하지 말 것):
```
SECRET_KEY=여기에_랜덤_64자_문자열_입력
```

생성 방법:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### HTTPS 강제 적용 (Nginx 사용 시)

```nginx
server {
    listen 80;
    server_name yourdomain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;

    location / {
        proxy_pass         http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection "upgrade";   # WebSocket 지원
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
```

### app.py 프로덕션 설정 추가

```python
# app.py 상단 config 부분에 추가
if os.environ.get("FLASK_ENV") == "production":
    app.config["SESSION_COOKIE_SECURE"]  = True   # HTTPS only
    app.config["SESSION_COOKIE_SAMESITE"]= "Strict"
```

---

## 4단계: C++ 엔진 빌드 오류 대처

C++ 컴파일이 실패해도 앱은 정상 동작합니다 (Python 폴백 모드).
컴파일 성공 여부는 메인 페이지 하단 푸터에서 확인 가능합니다.

| 환경 | 빌드 명령 |
|------|-----------|
| Linux (Ubuntu/Debian) | `g++ -O2 -shared -fPIC -o data_engine.so data_engine.cpp` |
| macOS | `g++ -O2 -shared -fPIC -o data_engine.so data_engine.cpp` |
| Windows (MSVC) | `cl /O2 /LD data_engine.cpp /Fe:data_engine.dll` |
| Windows (MinGW) | `g++ -O2 -shared -o data_engine.dll data_engine.cpp` |

---

## 5단계: 관리자 기능

| 기능 | URL |
|------|-----|
| 관리자 대시보드 | `/admin` |
| 회원 관리 (정지/활성화) | `/admin/users` |
| 게시물 삭제 (어드민 권한) | 게시물 상세 페이지 |

관리자 계정 승격 (DB 직접):
```bash
python -c "
from app import app, exec_db
with app.app_context():
    exec_db(\"UPDATE users SET role='admin' WHERE username=?\", ('원하는_아이디',))
    print('완료')
"
```

---

## 보안 기능 요약

| 항목 | 구현 내용 |
|------|-----------|
| 비밀번호 해싱 | PBKDF2-HMAC-SHA256, 26만 회 반복, 32바이트 Salt |
| 로그인 잠금 | 5회 실패 시 15분 잠금 |
| 로그인 기록 | IP, User-Agent, 성공/실패, 사유 저장 |
| 세션 보안 | HttpOnly, SameSite=Lax 쿠키, 12시간 만료 |
| 비밀번호 강도 | 8자+대소문자+숫자+특수문자 강제 |
| CSRF 방지 | SameSite 쿠키 + POST 전용 수정/삭제 라우트 |
| XSS 방지 | Jinja2 자동 이스케이프 + JS `escHtml()` |
| 파일 업로드 제한 | 5MB MAX_CONTENT_LENGTH |
| SQL 인젝션 방지 | 전 구간 파라미터 바인딩 |

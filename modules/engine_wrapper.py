"""
modules/engine_wrapper.py
C++ 데이터 엔진 Python 래퍼 (ctypes)
"""

import ctypes
import os
import platform

# ── 공유 라이브러리 로드 ──────────────────────────────────────────
_BASE = os.path.dirname(os.path.abspath(__file__))
_LIB_NAME = "data_engine.so" if platform.system() != "Windows" else "data_engine.dll"
_LIB_PATH = os.path.join(_BASE, _LIB_NAME)

try:
    _lib = ctypes.CDLL(_LIB_PATH)
    ENGINE_AVAILABLE = True
except OSError:
    ENGINE_AVAILABLE = False
    print(f"[WARN] C++ 엔진 로드 실패: {_LIB_PATH}. Python 폴백 사용.")

# ── 구조체 정의 ───────────────────────────────────────────────────

class CPost(ctypes.Structure):
    _fields_ = [
        ("id",            ctypes.c_int),
        ("title",         ctypes.c_char * 256),
        ("category",      ctypes.c_char * 64),
        ("author",        ctypes.c_char * 64),
        ("created_at",    ctypes.c_longlong),
        ("views",         ctypes.c_int),
        ("likes",         ctypes.c_int),
        ("comment_count", ctypes.c_int),
        ("score",         ctypes.c_float),
    ]

class CFilterOptions(ctypes.Structure):
    _fields_ = [
        ("category", ctypes.c_char * 64),
        ("keyword",  ctypes.c_char * 128),
        ("sort_by",  ctypes.c_int),
        ("order",    ctypes.c_int),
        ("limit",    ctypes.c_int),
        ("offset",   ctypes.c_int),
    ]

# ── 함수 시그니처 설정 ────────────────────────────────────────────
if ENGINE_AVAILABLE:
    _lib.filter_and_sort.restype  = ctypes.c_int
    _lib.filter_and_sort.argtypes = [
        ctypes.POINTER(CPost),
        ctypes.c_int,
        ctypes.POINTER(CFilterOptions),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
    ]
    _lib.top_n_posts.restype  = ctypes.c_int
    _lib.top_n_posts.argtypes = [
        ctypes.POINTER(CPost),
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int),
    ]
    _lib.engine_version.restype  = ctypes.c_char_p
    _lib.engine_version.argtypes = []

# ── 유틸: Python dict 목록 → CPost 배열 변환 ─────────────────────

def _to_cpost_array(post_dicts):
    arr = (CPost * len(post_dicts))()
    for i, p in enumerate(post_dicts):
        arr[i].id            = int(p.get("id", 0))
        arr[i].title         = str(p.get("title", "")).encode("utf-8")[:255]
        arr[i].category      = str(p.get("category", "")).encode("utf-8")[:63]
        arr[i].author        = str(p.get("author", "")).encode("utf-8")[:63]
        arr[i].created_at    = int(p.get("created_at", 0))
        arr[i].views         = int(p.get("views", 0))
        arr[i].likes         = int(p.get("likes", 0))
        arr[i].comment_count = int(p.get("comment_count", 0))
        arr[i].score         = 0.0
    return arr

# ── Python 폴백: 정렬/필터 ────────────────────────────────────────
import time as _time
import math as _math

def _python_score(p):
    now = _time.time()
    age_h = max(1.0, (now - p.get("created_at", 0)) / 3600.0)
    raw = p.get("likes", 0) * 3 + p.get("views", 0) * 0.1 + p.get("comment_count", 0) * 2
    return raw / (_math.sqrt(age_h) + 1.0)

def _python_filter_sort(post_dicts, category="", keyword="", sort_by=0,
                         order=0, limit=20, offset=0):
    cat_l = category.lower().strip()
    kw_l  = keyword.lower().strip()
    filtered = []
    for p in post_dicts:
        if cat_l and p.get("category", "").lower() != cat_l:
            continue
        if kw_l and kw_l not in p.get("title", "").lower():
            continue
        filtered.append(p)

    key_map = {0: "created_at", 1: "views", 2: "likes", 3: "_score"}
    for p in filtered:
        p["_score"] = _python_score(p)
    key = key_map.get(sort_by, "created_at")
    reverse = (order == 0)
    filtered.sort(key=lambda p: p.get(key, 0), reverse=reverse)

    total = len(filtered)
    sliced = filtered[offset: offset + limit]
    return [p["id"] for p in sliced], total

# ── 공개 API ─────────────────────────────────────────────────────

def filter_and_sort(post_dicts, category="", keyword="",
                    sort_by=0, order=0, limit=20, offset=0):
    """
    post_dicts: list of dict (id, title, category, author,
                               created_at, views, likes, comment_count)
    sort_by: 0=최신 1=조회수 2=좋아요 3=인기점수
    order:   0=내림차순 1=오름차순
    반환: (sorted_ids: list[int], total: int)
    """
    if not post_dicts:
        return [], 0

    if ENGINE_AVAILABLE:
        n = len(post_dicts)
        arr = _to_cpost_array(post_dicts)

        opts = CFilterOptions()
        opts.category = category.encode("utf-8")[:63]
        opts.keyword  = keyword.encode("utf-8")[:127]
        opts.sort_by  = sort_by
        opts.order    = order
        opts.limit    = limit
        opts.offset   = offset

        out_ids   = (ctypes.c_int * (n + 1))()
        out_total = ctypes.c_int(0)

        count = _lib.filter_and_sort(arr, n, ctypes.byref(opts),
                                     out_ids, ctypes.byref(out_total))
        return list(out_ids[:count]), out_total.value
    else:
        return _python_filter_sort(post_dicts, category, keyword,
                                    sort_by, order, limit, offset)


def top_n_posts(post_dicts, n=5):
    """인기 게시물 Top-N 반환 (id 리스트)"""
    if not post_dicts:
        return []
    if ENGINE_AVAILABLE:
        arr = _to_cpost_array(post_dicts)
        out = (ctypes.c_int * (n + 1))()
        count = _lib.top_n_posts(arr, len(post_dicts), n, out)
        return list(out[:count])
    else:
        for p in post_dicts:
            p["_score"] = _python_score(p)
        post_dicts.sort(key=lambda p: p["_score"], reverse=True)
        return [p["id"] for p in post_dicts[:n]]


def engine_version():
    if ENGINE_AVAILABLE:
        return _lib.engine_version().decode("utf-8")
    return "Python Fallback Engine v1.0.0"

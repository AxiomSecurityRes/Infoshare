/*
 * data_engine.cpp
 * 고성능 게시물 정렬/필터링 엔진
 * 컴파일: g++ -O2 -shared -fPIC -o data_engine.so data_engine.cpp
 */

#include <cstring>
#include <cstdlib>
#include <cstdio>
#include <algorithm>
#include <vector>
#include <string>
#include <ctime>

extern "C" {

/* ─────────────────────────────────────────────
   구조체 정의
───────────────────────────────────────────── */

struct Post {
    int    id;
    char   title[256];
    char   category[64];
    char   author[64];
    long long created_at;   // unix timestamp
    int    views;
    int    likes;
    int    comment_count;
    float  score;           // 정렬용 점수
};

struct FilterOptions {
    char   category[64];    // "" 이면 전체
    char   keyword[128];    // "" 이면 전체
    int    sort_by;         // 0=latest 1=views 2=likes 3=score
    int    order;           // 0=desc  1=asc
    int    limit;
    int    offset;
};

struct SortResult {
    int*   ids;             // 정렬된 id 배열
    int    count;
    int    total;           // 필터 후 전체 개수
};

/* ─────────────────────────────────────────────
   점수 계산 (Wilson score 변형)
   score = (likes + views*0.1 + comments*2) / age_hours^0.5
───────────────────────────────────────────── */
static float compute_score(const Post& p) {
    long long now = (long long)time(nullptr);
    double age_hours = (double)(now - p.created_at) / 3600.0;
    if (age_hours < 1.0) age_hours = 1.0;
    double raw = p.likes * 3.0 + p.views * 0.1 + p.comment_count * 2.0;
    return (float)(raw / (age_hours * 0.5 + 1.0));
}

/* ─────────────────────────────────────────────
   문자열 소문자 변환 (in-place)
───────────────────────────────────────────── */
static void to_lower(char* s) {
    for (; *s; ++s)
        if (*s >= 'A' && *s <= 'Z') *s += 32;
}

/* ─────────────────────────────────────────────
   메인 필터+정렬 함수
   posts     : Post 배열 포인터
   n         : 배열 크기
   opts      : 필터/정렬 옵션
   out_ids   : 결과 id를 담을 int 배열 (호출자가 충분히 할당해야 함)
   out_total : 필터 후 전체 개수 (페이지네이션용)
   반환값     : 실제 복사된 개수
───────────────────────────────────────────── */
int filter_and_sort(
    Post*          posts,
    int            n,
    FilterOptions* opts,
    int*           out_ids,
    int*           out_total
) {
    if (!posts || n <= 0 || !opts || !out_ids || !out_total) return 0;

    /* 점수 계산 */
    for (int i = 0; i < n; ++i)
        posts[i].score = compute_score(posts[i]);

    /* 필터링 */
    std::vector<int> indices;
    indices.reserve(n);

    char cat_lower[64];
    char kw_lower[128];
    strncpy(cat_lower, opts->category, 63); cat_lower[63] = '\0'; to_lower(cat_lower);
    strncpy(kw_lower,  opts->keyword,  127); kw_lower[127] = '\0'; to_lower(kw_lower);

    bool filter_cat = (cat_lower[0] != '\0');
    bool filter_kw  = (kw_lower[0]  != '\0');

    for (int i = 0; i < n; ++i) {
        /* 카테고리 필터 */
        if (filter_cat) {
            char tmp[64];
            strncpy(tmp, posts[i].category, 63); tmp[63] = '\0'; to_lower(tmp);
            if (strcmp(tmp, cat_lower) != 0) continue;
        }
        /* 키워드 필터 (제목 포함 검색) */
        if (filter_kw) {
            char tmp[256];
            strncpy(tmp, posts[i].title, 255); tmp[255] = '\0'; to_lower(tmp);
            if (strstr(tmp, kw_lower) == nullptr) continue;
        }
        indices.push_back(i);
    }

    *out_total = (int)indices.size();

    /* 정렬 */
    int sort_by = opts->sort_by;
    int order   = opts->order;

    std::stable_sort(indices.begin(), indices.end(),
        [&](int a, int b) -> bool {
            double va = 0, vb = 0;
            switch (sort_by) {
                case 0: va = (double)posts[a].created_at; vb = (double)posts[b].created_at; break;
                case 1: va = (double)posts[a].views;      vb = (double)posts[b].views;      break;
                case 2: va = (double)posts[a].likes;      vb = (double)posts[b].likes;      break;
                case 3: va = (double)posts[a].score;      vb = (double)posts[b].score;      break;
                default: va = (double)posts[a].created_at; vb = (double)posts[b].created_at;
            }
            return (order == 0) ? (va > vb) : (va < vb);
        }
    );

    /* 페이지네이션 & 결과 복사 */
    int offset = (opts->offset < 0) ? 0 : opts->offset;
    int limit  = (opts->limit  <= 0) ? 20 : opts->limit;
    int start  = std::min(offset, (int)indices.size());
    int end    = std::min(start + limit, (int)indices.size());
    int copied = 0;
    for (int i = start; i < end; ++i)
        out_ids[copied++] = posts[indices[i]].id;

    return copied;
}

/* ─────────────────────────────────────────────
   카테고리별 게시물 수 집계
   category_counts: [카테고리 인덱스] = 개수
   categories: 카테고리 이름 배열(외부 제공)
   n_cats: 카테고리 수
───────────────────────────────────────────── */
void count_by_category(
    Post*  posts,
    int    n,
    char** categories,
    int    n_cats,
    int*   category_counts
) {
    if (!posts || !categories || !category_counts) return;
    for (int c = 0; c < n_cats; ++c) category_counts[c] = 0;

    for (int i = 0; i < n; ++i) {
        char tmp[64];
        strncpy(tmp, posts[i].category, 63); tmp[63] = '\0'; to_lower(tmp);
        for (int c = 0; c < n_cats; ++c) {
            char cat[64];
            strncpy(cat, categories[c], 63); cat[63] = '\0'; to_lower(cat);
            if (strcmp(tmp, cat) == 0) { category_counts[c]++; break; }
        }
    }
}

/* ─────────────────────────────────────────────
   인기 게시물 Top-N 추출 (score 기준)
───────────────────────────────────────────── */
int top_n_posts(
    Post* posts,
    int   n,
    int   top_n,
    int*  out_ids
) {
    if (!posts || n <= 0 || top_n <= 0 || !out_ids) return 0;

    for (int i = 0; i < n; ++i)
        posts[i].score = compute_score(posts[i]);

    std::vector<int> idx(n);
    for (int i = 0; i < n; ++i) idx[i] = i;
    std::partial_sort(idx.begin(),
                      idx.begin() + std::min(top_n, n),
                      idx.end(),
                      [&](int a, int b){ return posts[a].score > posts[b].score; });

    int copied = std::min(top_n, n);
    for (int i = 0; i < copied; ++i)
        out_ids[i] = posts[idx[i]].id;
    return copied;
}

/* ─────────────────────────────────────────────
   버전 확인용
───────────────────────────────────────────── */
const char* engine_version() {
    return "InfoShare DataEngine v1.0.0 (C++)";
}

} // extern "C"

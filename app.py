import os
import re
import math
import time
import requests
import pandas as pd
import streamlit as st
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

# =========================
# Secrets / Keys
# =========================
YOUTUBE_API_KEY  = st.secrets.get("YOUTUBE_API_KEY", os.getenv("YOUTUBE_API_KEY", ""))
PEXELS_API_KEY   = st.secrets.get("PEXELS_API_KEY",  os.getenv("PEXELS_API_KEY",  ""))
REDDIT_UA        = st.secrets.get("REDDIT_USER_AGENT", os.getenv("REDDIT_USER_AGENT", "ShortsFinder/1.0 by minsuk"))

# =========================
# Endpoints / Const
# =========================
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
PEXELS_SEARCH_URL  = "https://api.pexels.com/videos/search"
PEXELS_POPULAR_URL = "https://api.pexels.com/videos/popular"
REQUEST_TIMEOUT    = 12
MAX_YT_PER_QUERY   = 500   # YouTube Search API 관례상 최대 500개 정도 반환

# =========================
# Helpers
# =========================
def http_get(url, params=None, headers=None, timeout=REQUEST_TIMEOUT, max_retries=2, backoff=0.8):
    last_err = None
    for i in range(max_retries + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception:
                    last_err = "Invalid JSON response"
            else:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            last_err = str(e)
        time.sleep(backoff * (2 ** i))
    raise RuntimeError(last_err or "Unknown network error")

def fmt_int(n):
    try:
        n = int(n)
    except:
        return n
    if n >= 1_000_000_000: return f"{n/1_000_000_000:.1f}B"
    if n >= 1_000_000:     return f"{n/1_000_000:.1f}M"
    if n >= 1_000:         return f"{n/1_000:.1f}K"
    return str(n)

def parse_iso8601_duration(s: str) -> int:
    if not s: return 0
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s)
    if not m: return 0
    h = int(m.group(1) or 0)
    mi = int(m.group(2) or 0)
    se = int(m.group(3) or 0)
    return h*3600 + mi*60 + se

def published_after_from_option(opt: str) -> str | None:
    now = datetime.now(timezone.utc)
    mapping = {
        "전체": None,
        "최근 24시간": now - timedelta(days=1),
        "최근 7일":    now - timedelta(days=7),
        "최근 30일":   now - timedelta(days=30),
        "최근 1년":    now - timedelta(days=365),
    }
    dt = mapping.get(opt)
    return dt.isoformat().replace("+00:00", "Z") if dt else None

def pexels_search_link(query: str) -> str:
    return f"https://www.pexels.com/search/videos/{quote_plus(query)}/"

# =========================
# Session
# =========================
def init_state():
    s = st.session_state
    s.setdefault("page", 1)
    s.setdefault("page_size", 10)
    s.setdefault("bookmarks", [])
    s.setdefault("search_history", [])
    s.setdefault("yt_sort", "조회수순")   # 또는 "최신순"
    s.setdefault("left_cols", 2)         # YT 카드 열 수
    s.setdefault("right_cols", 2)        # Pexels 카드 열 수
    s.setdefault("accent", "기본")
    s.setdefault("yt_fetch_limit", 100)  # YouTube 결과 수집량 기본

def page_controls(total_count: int, where: str):
    total_pages = max(1, math.ceil(total_count / st.session_state.page_size))
    cols = st.columns([1,1,2,2,1,1])
    with cols[0]:
        st.button("⏮️ 처음", key=f"{where}_first", on_click=lambda: st.session_state.update(page=1))
    with cols[1]:
        st.button("◀️ 이전", key=f"{where}_prev", on_click=lambda: st.session_state.update(page=max(1, st.session_state.page-1)))
    with cols[2]:
        # 표시 전용 (실제 변경은 사이드바)
        _ = st.selectbox(
            "한 페이지 보기",
            options=[10,20,50],
            index=[10,20,50].index(st.session_state.page_size) if st.session_state.page_size in (10,20,50) else 0,
            key=f"{where}_pagesize_display"
        )
    with cols[3]:
        st.session_state.page = st.number_input(
            f"페이지 (총 {total_pages}p)",
            min_value=1, max_value=total_pages, value=min(st.session_state.page, total_pages),
            step=1, key=f"{where}_pnum"
        )
    with cols[4]:
        st.button("다음 ▶️", key=f"{where}_next", on_click=lambda: st.session_state.update(page=min(total_pages, st.session_state.page+1)))
    with cols[5]:
        st.button("마지막 ⏭️", key=f"{where}_last", on_click=lambda: st.session_state.update(page=total_pages))

def slice_df_for_page(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    ps = st.session_state.page_size
    pg = st.session_state.page
    start = (pg - 1) * ps
    end = start + ps
    return df.iloc[start:end]

def render_cards(df: pd.DataFrame, *, cols: int, subtitles: list[str], bookmark_key_prefix: str):
    if df is None or df.empty:
        st.info("결과가 없습니다.")
        return
    rows = math.ceil(len(df) / cols)
    for r in range(rows):
        ccols = st.columns(cols)
        for i in range(cols):
            idx = r*cols + i
            if idx >= len(df): break
            row = df.iloc[idx].to_dict()
            with ccols[i]:
                if row.get("thumbnail"):
                    st.image(row["thumbnail"], use_container_width=True)
                title = row.get("title", "Untitled")
                url = row.get("url", "#")
                st.markdown(f"**[{title}]({url})**", unsafe_allow_html=True)

                chips = []
                for key in subtitles:
                    val = row.get(key)
                    if val in (None, "", 0): continue
                    if key == "author":          chips.append(f"제작자 {val}")
                    elif key == "views":         chips.append(f"조회수 {fmt_int(val)}")
                    elif key == "duration":      chips.append(f"길이 {val}s")
                    elif key == "durationText":  chips.append(f"길이 {val}")
                    elif key == "publishedAt":   chips.append(f"게시 {str(val)[:10]}")
                    elif key == "license":       chips.append(f"라이선스 {val}")
                if chips:
                    st.caption(" · ".join(chips))

                # 북마크
                book_id = f"{bookmark_key_prefix}_{idx}_{hash(url)%10_000_000}"
                def _add_bm(r=row):
                    st.session_state.bookmarks.append(r)
                    # 즉시 dedup
                    try:
                        bm_df = pd.DataFrame(st.session_state.bookmarks)
                        if "url" in bm_df.columns:
                            bm_df = bm_df.drop_duplicates(subset=["url"], keep="first")
                            st.session_state.bookmarks = bm_df.to_dict(orient="records")
                    except Exception:
                        pass
                st.button("⭐ 북마크", key=book_id, on_click=_add_bm)

def _filter_shorts(df: pd.DataFrame, flag: bool) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    if "isShorts" not in df.columns:
        return df.iloc[0:0]
    return df.loc[df["isShorts"] == flag]

# =========================
# Data Sources
# =========================
@st.cache_data(show_spinner=False, ttl=900)
def search_youtube(query: str, *, fetch_total: int, cc_only: bool, upload_window: str,
                   include_channels: list[str], exclude_channels: list[str],
                   include_channel_ids: list[str], exclude_channel_ids: list[str],
                   include_words: list[str], exclude_words: list[str],
                   region_code: str | None, relevance_lang: str | None,
                   safe_mode: str, order_mode: str,
                   duration_param: str,
                   min_seconds: int | None, max_seconds: int | None):
    """
    YouTube Search를 nextPageToken으로 반복 호출하여 최대 fetch_total개까지 수집.
    이후 videos API로 상세 정보(통계/길이) 배치 조회(50개 단위).
    """
    if not YOUTUBE_API_KEY:
        raise RuntimeError("YouTube API Key가 없습니다. secrets.toml에 YOUTUBE_API_KEY를 설정하세요.")

    fetch_total = max(1, min(int(fetch_total), MAX_YT_PER_QUERY))  # 1~500
    per_page = 50  # Search API 최대
    collected_ids = []
    page_token = None

    base_params = {
        "part": "snippet",
        "q": query,
        "maxResults": per_page,
        "type": "video",
        "key": YOUTUBE_API_KEY,
        "order": order_mode,  # "viewCount" or "date"
    }
    if cc_only:
        base_params["videoLicense"] = "creativeCommon"
    if region_code:
        base_params["regionCode"] = region_code
    if relevance_lang:
        base_params["relevanceLanguage"] = relevance_lang
    if safe_mode in ("none","moderate","strict"):
        base_params["safeSearch"] = safe_mode
    if duration_param in ("any","short","medium","long") and duration_param != "any":
        base_params["videoDuration"] = duration_param

    pub_after = published_after_from_option(upload_window)
    if pub_after:
        base_params["publishedAfter"] = pub_after

    # ------- Search 루프 (최대 fetch_total 또는 nextPageToken 소진) -------
    while len(collected_ids) < fetch_total:
        params = dict(base_params)
        if page_token:
            params["pageToken"] = page_token

        sjson = http_get(YOUTUBE_SEARCH_URL, params=params)
        items = sjson.get("items", [])
        ids = [it.get("id", {}).get("videoId") for it in items if it.get("id", {}).get("videoId")]
        if not ids:
            break
        collected_ids.extend(ids)

        page_token = sjson.get("nextPageToken")
        if not page_token:
            break

        # 안전: 너무 큰 루프 방지
        if len(collected_ids) >= fetch_total:
            break

    # 필요량으로 자르기
    collected_ids = list(dict.fromkeys(collected_ids))[:fetch_total]
    if not collected_ids:
        return []

    # ------- Videos 상세 조회 (50개 단위) -------
    out = []
    for i in range(0, len(collected_ids), 50):
        chunk = collected_ids[i:i+50]
        params2 = {
            "part": "snippet,statistics,contentDetails",
            "id": ",".join(chunk),
            "key": YOUTUBE_API_KEY
        }
        vjson = http_get(YOUTUBE_VIDEOS_URL, params=params2)
        for v in vjson.get("items", []):
            vid = v["id"]
            sn  = v.get("snippet", {})
            stt = v.get("statistics", {})
            cd  = v.get("contentDetails", {})
            thumbs = sn.get("thumbnails", {})
            thumb = (thumbs.get("medium") or thumbs.get("high") or thumbs.get("default") or {}).get("url")

            channel_title = sn.get("channelTitle", "")
            channel_id    = sn.get("channelId", "")
            seconds = parse_iso8601_duration(cd.get("duration"))
            is_shorts = seconds <= 60 if seconds else False

            title_l = (sn.get("title") or "").lower()

            # 포함/제외 필터링
            if include_words and not all(w.lower() in title_l for w in include_words):
                continue
            if exclude_words and any(w.lower() in title_l for w in exclude_words):
                continue
            if include_channels and channel_title not in include_channels:
                continue
            if exclude_channels and channel_title in exclude_channels:
                continue
            if include_channel_ids and channel_id not in include_channel_ids:
                continue
            if exclude_channel_ids and channel_id in exclude_channel_ids:
                continue
            if (min_seconds is not None and seconds < min_seconds) or (max_seconds is not None and seconds > max_seconds):
                continue

            out.append({
                "platform": "YouTube",
                "title": sn.get("title"),
                "author": channel_title,
                "views": int(stt.get("viewCount", 0)) if stt.get("viewCount") else None,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "thumbnail": thumb,
                "publishedAt": sn.get("publishedAt"),
                "durationSec": seconds,
                "durationText": str(timedelta(seconds=seconds)) if seconds else "",
                "isShorts": is_shorts,
                "license": "CC" if cc_only else "Standard/Unknown",
            })

    # 표시 정렬(보조)
    if st.session_state.yt_sort == "조회수순":
        out.sort(key=lambda x: (x["views"] or -1), reverse=True)
    else:
        out.sort(key=lambda x: x.get("publishedAt", "") or "", reverse=True)

    # URL 기준 dedup (혹시 모를 중복 제거)
    seen = set()
    deduped = []
    for r in out:
        u = r.get("url")
        if u and u not in seen:
            deduped.append(r)
            seen.add(u)
    return deduped

@st.cache_data(show_spinner=False, ttl=900)
def search_pexels(query: str, *, fetch: int, mode="search"):
    if not PEXELS_API_KEY:
        return []
    headers = {"Authorization": PEXELS_API_KEY}
    url = PEXELS_POPULAR_URL if mode == "popular" else PEXELS_SEARCH_URL
    params = {"per_page": min(fetch, 80)}
    if mode == "search":
        params["query"] = query
    data = http_get(url, params=params, headers=headers)
    out = []
    for v in data.get("videos", []):
        out.append({
            "platform": "Pexels",
            "title": f"Pexels Video by {v.get('user',{}).get('name','Creator')}",
            "author": v.get("user",{}).get("name"),
            "views": None,
            "url": v.get("url"),
            "thumbnail": v.get("image"),
            "duration": v.get("duration"),
        })
    return out

# =========================
# UI
# =========================
st.set_page_config(page_title="민석이의 작업실", page_icon="🛠️", layout="wide")
init_state()

# Accent Theme (CSS)
accent_map = {
    "기본": "#00A389",
    "블루": "#2F80ED",
    "그린": "#27AE60",
    "핑크": "#EB5757",
    "보라": "#A259FF",
}
accent = accent_map.get(st.session_state.accent, "#00A389")

# ---- CSS (제목 잘림 방지 + 테마 색) ----
st.markdown(f"""
<style>
:root {{ --accent: {accent}; }}
.block-container {{ padding-top: 24px; }}
.v-sep {{ border-left: 1px solid #ddd; height: 100%; opacity: 0.4; }}
h1, .stApp h1 {{ margin: 8px 0 6px 0 !important; line-height: 1.28; }}
a {{ color: var(--accent) !important; }}
.stButton>button, .stDownloadButton button {{
  border-radius: 999px; border: 1px solid var(--accent);
  color: var(--accent); background: transparent;
}}
.stButton>button:hover, .stDownloadButton button:hover {{
  background: var(--accent); color: white;
}}
</style>
""", unsafe_allow_html=True)

# 제목 (스페이서 + 타이틀)
st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
st.title("🛠️ 민석이의 작업실")
st.caption("원본 링크로만 이동하는 안전한 개인용 검색 도구 (다운로드 기능 없음)")

# ===== 사이드바 (한글 UI, 모든 위젯에 key 부여) =====
with st.sidebar:
    st.header("검색 옵션")

    # 최근 검색
    if st.session_state.search_history:
        st.caption("최근 검색")
        hist_cols = st.columns(3)
        for i, qv in enumerate(st.session_state.search_history[-9:][::-1]):
            with hist_cols[i % 3]:
                if st.button(qv, key=f"hist_{i}"):
                    st.session_state["_prefill_query"] = qv

    # 검색어 + (맨 위 실행 버튼)
    q_default = st.session_state.get("_prefill_query", "prank")
    query = st.text_input("검색 키워드", value=q_default, key="sb_query")
    st.session_state["_prefill_query"] = query

    run = st.button("검색 실행", use_container_width=True, type="primary", key="run_top")

    st.markdown("---")

    # 화면 배치
    layout_mode = st.selectbox("화면 배치", ["가로 2패널", "상하 스택"], index=0, key="sb_layout")

    # 민감도(YouTube safeSearch)
    safe_label = st.selectbox("민감도 필터(YouTube)", ["강함", "보통", "해제"], index=1, key="sb_safe")
    yt_safe = {"강함": "strict", "보통": "moderate", "해제": "none"}[safe_label]

    # YouTube 설정
    st.subheader("YouTube 설정")
    yt_cc   = st.checkbox("크리에이티브 커먼즈(CC)만", value=False, key="sb_cc")
    yt_upload_window = st.selectbox("업로드 시점",
                                    ["전체", "최근 24시간", "최근 7일", "최근 30일", "최근 1년"],
                                    index=0, key="sb_uploadwin")

    # 지역/언어 (한국어 라벨 → 코드 매핑)
    region_label = st.selectbox("지역",
                                ["자동", "한국(KR)", "미국(US)", "일본(JP)", "영국(GB)", "독일(DE)", "프랑스(FR)", "인도(IN)", "인도네시아(ID)", "브라질(BR)", "멕시코(MX)"],
                                index=1, key="sb_region")
    region_map = {"자동":"", "한국(KR)":"KR", "미국(US)":"US", "일본(JP)":"JP", "영국(GB)":"GB", "독일(DE)":"DE", "프랑스(FR)":"FR",
                  "인도(IN)":"IN", "인도네시아(ID)":"ID", "브라질(BR)":"BR", "멕시코(MX)":"MX"}
    region_code = region_map[region_label]

    lang_label = st.selectbox("언어",
                              ["자동", "한국어(ko)", "영어(en)", "일본어(ja)", "스페인어(es)", "프랑스어(fr)", "독일어(de)", "인도네시아어(id)", "포르투갈어(pt)", "힌디어(hi)"],
                              index=1, key="sb_lang")
    lang_map = {"자동":"", "한국어(ko)":"ko", "영어(en)":"en", "일본어(ja)":"ja", "스페인어(es)":"es", "프랑스어(fr)":"fr", "독일어(de)":"de",
                "인도네시아어(id)":"id", "포르투갈어(pt)":"pt", "힌디어(hi)":"hi"}
    relevance_lang = lang_map[lang_label]

    # 길이 필터 (라벨→API 값)
    ylen_label = st.selectbox("길이 필터", ["전체", "짧음(<4분)", "중간(4~20분)", "긴(>20분)"], index=0, key="sb_ylen")
    ylen_map = {"전체":"any", "짧음(<4분)":"short", "중간(4~20분)":"medium", "긴(>20분)":"long"}
    duration_param = ylen_map[ylen_label]

    # 초 단위 길이 범위
    c1, c2 = st.columns(2)
    with c1:
        min_sec = st.number_input("최소 길이(초)", min_value=0, max_value=86400, value=0, step=5, key="sb_minsec")
        min_seconds = None if min_sec == 0 else int(min_sec)
    with c2:
        max_sec = st.number_input("최대 길이(초)", min_value=0, max_value=86400, value=0, step=5, key="sb_maxsec")
        max_seconds = None if max_sec == 0 else int(max_sec)

    # 채널/키워드 필터
    inc = st.text_input("포함 채널명(쉼표)", value="", key="sb_inc_chname")
    exc = st.text_input("제외 채널명(쉼표)", value="", key="sb_exc_chname")
    inc_ids = st.text_input("포함 채널ID(쉼표)", value="", key="sb_inc_chid")
    exc_ids = st.text_input("제외 채널ID(쉼표)", value="", key="sb_exc_chid")
    include_channels = [s.strip() for s in inc.split(",") if s.strip()]
    exclude_channels = [s.strip() for s in exc.split(",") if s.strip()]
    include_channel_ids = [s.strip() for s in inc_ids.split(",") if s.strip()]
    exclude_channel_ids = [s.strip() for s in exc_ids.split(",") if s.strip()]

    inc_words = st.text_input("제목에 반드시 포함(쉼표)", value="", key="sb_inc_words")
    exc_words = st.text_input("제목에 포함되면 제외(쉼표)", value="", key="sb_exc_words")
    include_words = [s.strip() for s in inc_words.split(",") if s.strip()]
    exclude_words = [s.strip() for s in exc_words.split(",") if s.strip()]

    # YouTube 결과 수집량
    st.subheader("YouTube 결과 수집량")
    st.session_state.yt_fetch_limit = st.selectbox(
        "최대 가져올 개수",
        options=[50,100,200,300,400,500],
        index=[50,100,200,300,400,500].index(st.session_state.yt_fetch_limit) if st.session_state.yt_fetch_limit in [50,100,200,300,400,500] else 1,
        key="sb_fetch_limit"
    )

    st.markdown("---")

    # 페이지 크기 (사이드바에서만 상태 변경)
    st.subheader("한 페이지 보기")
    st.session_state.page_size = st.selectbox("표시 개수", [10,20,50],
                                              index=[10,20,50].index(st.session_state.page_size),
                                              key="sb_pagesize")

    st.markdown("---")

    # Pexels 설정
    st.subheader("Pexels 설정")
    px_mode_label = st.selectbox("결과 유형", ["검색 결과", "인기 동영상"], index=0, key="sb_pxmode")
    px_mode = "search" if px_mode_label == "검색 결과" else "popular"

    st.markdown("---")

    # 테마 색상 선택
    st.subheader("포인트 색상")
    st.session_state.accent = st.selectbox("색상", ["기본","블루","그린","핑크","보라"],
                                           index=["기본","블루","그린","핑크","보라"].index(st.session_state.accent),
                                           key="sb_accent")

# ===== 상단 툴바(정렬/열수) - 모두 key 부여 =====
with st.container():
    tb1, tb2, tb3, tb4 = st.columns([1.3,1,1,1])
    with tb1:
        st.session_state.yt_sort = st.radio("YouTube 정렬", ["조회수순","최신순"],
                                            horizontal=True,
                                            index=["조회수순","최신순"].index(st.session_state.yt_sort),
                                            key="tb_sort")
    with tb2:
        st.session_state.left_cols = st.select_slider("YouTube 열수", options=[2,3],
                                                      value=st.session_state.left_cols, key="tb_left_cols")
    with tb3:
        st.session_state.right_cols = st.select_slider("Pexels 열수", options=[2,3],
                                                       value=st.session_state.right_cols, key="tb_right_cols")
    with tb4:
        st.write("")
        st.caption("정렬과 열수는 상단에서 빠르게 바꿔요 ✨")

# =========================
# 검색 실행
# =========================
yt_df_all = pd.DataFrame()
px_df_all = pd.DataFrame()

if run and query:
    # 최근 검색어 저장 (10개 유지)
    if query not in st.session_state.search_history:
        st.session_state.search_history.append(query)
    if len(st.session_state.search_history) > 10:
        st.session_state.search_history = st.session_state.search_history[-10:]

    with st.spinner("검색 중…"):
        # YouTube
        try:
            order_mode = "viewCount" if st.session_state.yt_sort == "조회수순" else "date"
            yt_all = search_youtube(
                query,
                fetch_total=st.session_state.yt_fetch_limit,
                cc_only=yt_cc,
                upload_window=yt_upload_window,
                include_channels=include_channels,
                exclude_channels=exclude_channels,
                include_channel_ids=include_channel_ids,
                exclude_channel_ids=exclude_channel_ids,
                include_words=include_words,
                exclude_words=exclude_words,
                region_code=(region_code or None),
                relevance_lang=(relevance_lang or None),
                safe_mode=yt_safe,
                order_mode=order_mode,
                duration_param=duration_param,
                min_seconds=min_seconds,
                max_seconds=max_seconds,
            )
            yt_df_all = pd.DataFrame(yt_all)
        except Exception as e:
            st.error(f"YouTube 검색 오류: {e}")

        # Pexels (오른쪽 패널)
        try:
            if PEXELS_API_KEY:
                px_all = search_pexels(query, fetch=max(50, st.session_state.page_size), mode=px_mode)
                px_df_all = pd.DataFrame(px_all)
            else:
                px_df_all = pd.DataFrame()
        except Exception as e:
            st.error(f"Pexels 검색 오류: {e}")

    # 페이지네이션 (상단)
    total_rows = max(len(yt_df_all), len(px_df_all))
    page_controls(total_rows, where="top")

    yt_df_page = slice_df_for_page(yt_df_all) if not yt_df_all.empty else yt_df_all
    px_df_page = slice_df_for_page(px_df_all) if not px_df_all.empty else px_df_all

    # ===== 레이아웃 렌더링 =====
    def render_two_panels():
        left, sep, right = st.columns([1, 0.03, 1], gap="large")
        with left:
            st.subheader("🎬 YouTube")
            if yt_df_page.empty:
                st.info("YouTube 결과가 없습니다.")
            else:
                tab_all, tab_shorts, tab_video = st.tabs(["전체","쇼츠","영상"])
                with tab_all:
                    render_cards(yt_df_page, cols=st.session_state.left_cols,
                                 subtitles=["author","views","durationText","publishedAt","license"],
                                 bookmark_key_prefix="yt")
                with tab_shorts:
                    df_s = _filter_shorts(yt_df_page, True)
                    render_cards(df_s, cols=st.session_state.left_cols,
                                 subtitles=["author","views","durationText","publishedAt","license"],
                                 bookmark_key_prefix="yt_s")
                with tab_video:
                    df_v = _filter_shorts(yt_df_page, False)
                    render_cards(df_v, cols=st.session_state.left_cols,
                                 subtitles=["author","views","durationText","publishedAt","license"],
                                 bookmark_key_prefix="yt_v")
        with sep:
            st.markdown("<div class='v-sep'>&nbsp;</div>", unsafe_allow_html=True)
        with right:
            st.subheader("📹 Pexels")
            if PEXELS_API_KEY and not px_df_page.empty:
                render_cards(px_df_page, cols=st.session_state.right_cols,
                             subtitles=["author","duration"], bookmark_key_prefix="px")
            else:
                st.info("Pexels API 키가 없거나 결과가 없습니다.")
                if query:
                    st.link_button("🔗 Pexels에서 검색하기", pexels_search_link(query), use_container_width=True)
                st.caption("※ 목록을 앱에서 보려면 PEXELS_API_KEY를 설정하세요.")

    def render_stacked():
        st.subheader("🎬 YouTube")
        if yt_df_page.empty:
            st.info("YouTube 결과가 없습니다.")
        else:
            tab_all, tab_shorts, tab_video = st.tabs(["전체","쇼츠","영상"])
            with tab_all:
                render_cards(yt_df_page, cols=3,
                             subtitles=["author","views","durationText","publishedAt","license"],
                             bookmark_key_prefix="yt")
            with tab_shorts:
                df_s = _filter_shorts(yt_df_page, True)
                render_cards(df_s, cols=3,
                             subtitles=["author","views","durationText","publishedAt","license"],
                             bookmark_key_prefix="yt_s")
            with tab_video:
                df_v = _filter_shorts(yt_df_page, False)
                render_cards(df_v, cols=3,
                             subtitles=["author","views","durationText","publishedAt","license"],
                             bookmark_key_prefix="yt_v")
        st.markdown("---")
        st.subheader("📹 Pexels")
        if PEXELS_API_KEY and not px_df_page.empty:
            render_cards(px_df_page, cols=3, subtitles=["author","duration"], bookmark_key_prefix="px")
        else:
            st.info("Pexels API 키가 없거나 결과가 없습니다.")
            if query:
                st.link_button("🔗 Pexels에서 검색하기", pexels_search_link(query), use_container_width=True)
            st.caption("※ 목록을 앱에서 보려면 PEXELS_API_KEY를 설정하세요.")

    if layout_mode == "가로 2패널":
        render_two_panels()
    else:
        render_stacked()

    # 페이지네이션 (하단)
    page_controls(total_rows, where="bottom")

    st.markdown("---")

    # 북마크 & 내보내기
    st.subheader("⭐ 북마크")
    if st.session_state.bookmarks:
        bm_df = pd.DataFrame(st.session_state.bookmarks)
        if "url" in bm_df.columns:
            bm_df = bm_df.drop_duplicates(subset=["url"], keep="first")
        cols_to_show = [c for c in ["platform","title","author","views","url","durationText","publishedAt","license","duration"] if c in bm_df.columns]
        st.dataframe(bm_df[cols_to_show].fillna(""), use_container_width=True, height=260)
        st.download_button(
            "북마크 CSV 다운로드",
            bm_df.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"minsuk_lab_bookmarks_{int(time.time())}.csv", mime="text/csv"
        )
    else:
        st.caption("아직 북마크가 없어요. 카드의 ⭐ 버튼을 눌러 추가하세요.")

# Footer legal
st.markdown("<p style='font-variant-caps: all-small-caps; letter-spacing: .03em; color: #777;'>본 도구는 원본 링크로만 이동하며 미디어 다운로드 기능을 제공하지 않습니다.</p>", unsafe_allow_html=True)

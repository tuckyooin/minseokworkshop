# app.py
import os
import re
import io
import math
import time
import random
import requests
import pandas as pd
import streamlit as st
from datetime import datetime, timedelta, timezone
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# =========================
# Robust HTTP Session (재시도/연결 재사용)
# =========================
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "MinsukSearch/3.2", "Accept-Encoding": "gzip"})
_retry = Retry(
    total=3, connect=3, read=3,
    backoff_factor=0.7,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET","POST"]
)
_adapter = HTTPAdapter(max_retries=_retry, pool_connections=20, pool_maxsize=60)
SESSION.mount("http://", _adapter)
SESSION.mount("https://", _adapter)

# =========================
# Optional OCR
# =========================
try:
    from PIL import Image
    import pytesseract
    HAS_OCR = True
except Exception:
    HAS_OCR = False

# =========================
# Keys / Const
# =========================
def _split_keys(s: str) -> list[str]:
    return [k.strip() for k in (s or "").split(",") if k.strip()]

# 여러 개 키를 콤마로 넣을 수 있음: YOUTUBE_API_KEY="키A,키B,키C"
YOUTUBE_API_KEYS = _split_keys(st.secrets.get("YOUTUBE_API_KEY", os.getenv("YOUTUBE_API_KEY", "")))
DEEPL_API_KEY    = st.secrets.get("DEEPL_API_KEY", os.getenv("DEEPL_API_KEY", ""))

CSE_API_KEY      = st.secrets.get("CSE_API_KEY", os.getenv("CSE_API_KEY", ""))
CSE_CX           = st.secrets.get("CSE_CX", os.getenv("CSE_CX", ""))

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
REQUEST_TIMEOUT    = 12
MAX_YT_PER_QUERY   = 500
PAGE_SIZE_FIXED    = 15  # 한 페이지 15 고정

# =========================
# Helpers
# =========================
def http_get(url, params=None, headers=None, timeout=REQUEST_TIMEOUT):
    r = SESSION.get(url, params=params, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()

def http_get_bytes(url, timeout=10):
    r = SESSION.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content

def yt_get(url: str, params: dict, timeout=REQUEST_TIMEOUT):
    """
    YouTube API 호출 시 키 자동 로테이션.
    quotaExceeded/403 등 발생하면 다음 키로 변경하여 재시도.
    성공한 키 인덱스를 세션에 고정.
    """
    if not YOUTUBE_API_KEYS:
        raise RuntimeError("YouTube API Key가 없습니다. secrets.toml에 YOUTUBE_API_KEY를 설정하세요.")
    last_err = None
    start_idx = st.session_state.get("yt_key_idx", 0)
    for offset in range(len(YOUTUBE_API_KEYS)):
        idx = (start_idx + offset) % len(YOUTUBE_API_KEYS)
        key = YOUTUBE_API_KEYS[idx]
        p = dict(params); p["key"] = key
        try:
            r = SESSION.get(url, params=p, timeout=timeout)
            if r.status_code == 403:
                try:
                    js = r.json()
                    reason = js.get("error", {}).get("errors", [{}])[0].get("reason", "")
                    if reason == "quotaExceeded":
                        st.warning(f"YouTube 키 #{idx+1} 쿼터 소진. 다음 키로 전환합니다.")
                        continue
                except Exception:
                    pass
            r.raise_for_status()
            st.session_state["yt_key_idx"] = idx
            return r.json()
        except requests.exceptions.HTTPError as e:
            last_err = e
            continue
        except Exception as e:
            last_err = e
            continue
    if last_err:
        raise last_err
    raise RuntimeError("YouTube API 요청 실패(원인 불명)")

def fmt_int(n):
    try: n = int(n)
    except: return n
    if n >= 1_000_000_000: return f"{n/1_000_000_000:.1f}B"
    if n >= 1_000_000:     return f"{n/1_000_000:.1f}M"
    if n >= 1_000:         return f"{n/1_000:.1f}K"
    return str(n)

def parse_iso8601_duration(s: str) -> int:
    if not s: return 0
    m = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", s)
    if not m: return 0
    h = int(m.group(1) or 0); mi = int(m.group(2) or 0); se = int(m.group(3) or 0)
    return h*3600 + mi*60 + se

def published_after_from_option(opt: str) -> str | None:
    now = datetime.now(timezone.utc)
    mapping = {
        "전체": None, "최근 24시간": now - timedelta(days=1),
        "최근 7일": now - timedelta(days=7), "최근 30일": now - timedelta(days=30),
        "최근 1년": now - timedelta(days=365),
    }
    dt = mapping.get(opt)
    return dt.isoformat().replace("+00:00","Z") if dt else None

def compute_engagement_score(stats: dict) -> float:
    try:
        v = max(1, int(stats.get("viewCount", 0) or 0))
        l = int(stats.get("likeCount", 0) or 0)
        c = int(stats.get("commentCount", 0) or 0)
        return (l + c) / (v ** 0.85)
    except Exception:
        return 0.0

# ---------- 번역 유틸 ----------
def has_hangul(s: str) -> bool:
    return bool(re.search(r"[가-힣]", s or ""))

def translate_to_ko(text: str) -> str:
    try:
        if not text or has_hangul(text): return text
        if DEEPL_API_KEY:
            url = "https://api-free.deepl.com/v2/translate"
            data = {"auth_key": DEEPL_API_KEY, "text": text, "target_lang": "KO"}
            r = SESSION.post(url, data=data, timeout=10)
            if r.status_code == 200:
                js = r.json()
                trs = js.get("translations", [])
                if trs: return trs[0].get("text", text)
        js = http_get("https://api.mymemory.translated.net/get", {"q": text, "langpair": "en|ko"}, timeout=10)
        if js and js.get("responseData", {}).get("translatedText"):
            return js["responseData"]["translatedText"]
    except Exception:
        pass
    return text

def translate_block_to_ko(text: str) -> str:
    if not text: return text
    parts = re.split(r"(\n{2,})", text)
    out = []
    for p in parts:
        if p.strip()=="" or p.startswith("\n"):
            out.append(p)
        else:
            out.append(translate_to_ko(p))
            time.sleep(0.1)
    return "".join(out)

# =========================
# Age keyword map
# =========================
GE_KEYWORDS = {
    10: ["minecraft","roblox","포켓몬","애니","게임","마인크래프트","틴","학생","공부법","고등학교","중학교","초등학교","로블록스"],
    20: ["대학생","브이로그","여행","취업","자취","카페","패션","아이돌","kpop","연예"],
    30: ["직장인","육아","재테크","부동산","인테리어","홈카페","저축","요리","헬스"],
    40: ["건강","퇴직","가족","골프","등산","주택","가전","보험","클래식"],
    50: ["건강검진","관절","은퇴","취미","가드닝","캠핑","요리","여행"],
    60: ["시니어","건강","복지","정원","텃밭","낚시","국악","교양","역사","트로트","연금","노후"],
}
AGE_KEYWORDS = {
    "10대": GE_KEYWORDS[10],
    "20대": GE_KEYWORDS[20],
    "30대": GE_KEYWORDS[30],
    "40대": GE_KEYWORDS[40],
    "50대": GE_KEYWORDS[50],
    "60대": GE_KEYWORDS[60],
}

GENERIC_GAME_NEG = [
    "game","게임","겜","마인크래프트","minecraft","roblox","로블록스","포켓몬",
    "fortnite","genshin","원신","lol","리그 오브 레전드","valorant","발로란트",
    "배그","pubg","steam","스팀","xbox","ps5","플스","닌텐도","nintendo","switch","스위치"
]

def build_age_neg_keywords():
    tags = list(AGE_KEYWORDS.keys())
    neg = {}
    for t in tags:
        others = []
        for t2 in tags:
            if t2 == t: continue
            others.extend(AGE_KEYWORDS[t2])
        neg[t] = sorted(set([o.lower() for o in others]))
    return neg

AGE_NEG_KEYWORDS = build_age_neg_keywords()

def age_relevance_score(title: str, age_tag: str) -> int:
    if not title or age_tag not in AGE_KEYWORDS: return 0
    t = title.lower()
    score = 0
    for kw in AGE_KEYWORDS[age_tag]:
        if kw.lower() in t:
            score += 1
    return score

def age_negative_hit(title: str, age_tag: str) -> bool:
    if not title or age_tag not in AGE_NEG_KEYWORDS: return False
    t = title.lower()
    if any(neg in t for neg in AGE_NEG_KEYWORDS[age_tag]):
        return True
    if age_tag != "10대" and any(kw in t for kw in [g.lower() for g in GENERIC_GAME_NEG]):
        return True
    return False

# =========================
# Session
# =========================
def init_state():
    s = st.session_state
    s.setdefault("page", 1)
    s["page_size"] = PAGE_SIZE_FIXED
    s.setdefault("search_history", [])
    s.setdefault("yt_sort", "조회수순")
    s.setdefault("accent", "기본")
    s.setdefault("yt_fetch_limit", 100)
    s.setdefault("analysis_target", None)
    s.setdefault("trigger_analysis", False)
    s.setdefault("reco_clicks", 0)
    s.setdefault("results_df", pd.DataFrame())
    s.setdefault("last_query", "")
    s.setdefault("last_params", {})
    s.setdefault("age_filter", "전체")
    s.setdefault("region_code", "KR")
    s.setdefault("do_search_now", False)
    s.setdefault("yt_key_idx", 0)  # 현재 사용하는 키 인덱스

    # ✅ 새로 추가: 수동 호출 모드 & 원할 때만 로드
    s.setdefault("manual_api_mode", True)
    s.setdefault("want_reco_now", False)         # 추천 로드 버튼 신호
    s.setdefault("want_kwboard_now", False)      # 키워드 TOP 로드 버튼 신호

# =========================
# Units Estimator (표시용)
# =========================
def estimate_units_for_youtube_search(fetch_total: int) -> int:
    """ search.list: 100 units/page(50개) + videos.list: 1 unit/50개 """
    ft = max(1, min(int(fetch_total), MAX_YT_PER_QUERY))
    pages = math.ceil(ft / 50)         # search.list 호출 수
    v_chunks = math.ceil(ft / 50)      # videos.list 호출 수
    return pages * 100 + v_chunks * 1  # 단순 상한 추정

def estimate_units_for_trending(fetch_total: int = 200) -> int:
    """ videos.list(chart=mostPopular) 1 unit/page """
    pages = math.ceil(fetch_total / 50)
    return pages * 1

def estimate_units_for_kwboard(per_keyword: int = 6, keywords: int = 8) -> int:
    """ keyword_ranked_recos 내부: 각 키워드당 120개 수집 (search 3p *100 + videos 3*1) """
    search_pages_per_kw = 3  # 120/50 반올림
    return keywords * (search_pages_per_kw * 100 + search_pages_per_kw * 1)

# =========================
# Data: YouTube
# =========================
@st.cache_data(show_spinner=False, ttl=900)
def search_youtube(query: str, *, fetch_total: int, cc_only: bool, upload_window: str,
                   include_channels: list[str], exclude_channels: list[str],
                   include_channel_ids: list[str], exclude_channel_ids: list[str],
                   include_words: list[str], exclude_words: list[str],
                   region_code: str | None, relevance_lang: str | None,
                   safe_mode: str, order_mode: str,
                   duration_param: str,
                   min_seconds: int | None, max_seconds: int | None,
                   age_tag: str = "전체"):
    if not YOUTUBE_API_KEYS:
        raise RuntimeError("YouTube API Key가 없습니다. secrets.toml에 YOUTUBE_API_KEY를 설정하세요.")

    fetch_total = max(1, min(int(fetch_total), MAX_YT_PER_QUERY))
    per_page = 50
    collected_ids, page_token = [], None

    base_params = {
        "part": "snippet", "q": query, "maxResults": per_page,
        "type": "video", "order": order_mode,
    }
    if cc_only: base_params["videoLicense"] = "creativeCommon"
    if region_code: base_params["regionCode"] = region_code
    if relevance_lang: base_params["relevanceLanguage"] = relevance_lang
    if safe_mode in ("none","moderate","strict"): base_params["safeSearch"] = safe_mode
    if duration_param in ("short","medium","long"): base_params["videoDuration"] = duration_param
    pub_after = published_after_from_option(upload_window)
    if pub_after: base_params["publishedAfter"] = pub_after

    while len(collected_ids) < fetch_total:
        params = dict(base_params)
        if page_token: params["pageToken"] = page_token
        sjson = yt_get(YOUTUBE_SEARCH_URL, params=params)
        items = sjson.get("items", [])
        ids = [it.get("id", {}).get("videoId") for it in items if it.get("id", {}).get("videoId")]
        if not ids: break
        collected_ids.extend(ids)
        page_token = sjson.get("nextPageToken")
        if not page_token or len(collected_ids) >= fetch_total: break

    collected_ids = list(dict.fromkeys(collected_ids))[:fetch_total]
    if not collected_ids: return []

    out = []
    for i in range(0, len(collected_ids), 50):
        chunk = collected_ids[i:i+50]
        params2 = {"part": "snippet,statistics,contentDetails", "id": ",".join(chunk)}
        vjson = yt_get(YOUTUBE_VIDEOS_URL, params=params2)
        for v in vjson.get("items", []):
            vid = v["id"]
            sn  = v.get("snippet", {})
            stt = v.get("statistics", {})
            cd  = v.get("contentDetails", {})
            thumbs = sn.get("thumbnails", {})
            thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url")
            seconds = parse_iso8601_duration(cd.get("duration"))
            is_shorts = seconds <= 60 if seconds else False

            title = sn.get("title") or ""
            channel_title = sn.get("channelTitle", "")
            channel_id = sn.get("channelId", "")
            title_l = title.lower()

            # 필터링
            if include_words and not all(w.lower() in title_l for w in include_words): continue
            if exclude_words and any(w.lower() in title_l for w in exclude_words): continue
            if include_channels and channel_title not in include_channels: continue
            if exclude_channels and channel_title in exclude_channels: continue
            if include_channel_ids and channel_id not in include_channel_ids: continue
            if exclude_channel_ids and channel_id in exclude_channel_ids: continue
            if (min_seconds is not None and seconds < min_seconds) or (max_seconds is not None and seconds > max_seconds): continue

            out.append({
                "platform": "YouTube",
                "title": title,
                "author": channel_title,
                "views": int(stt.get("viewCount", 0)) if stt.get("viewCount") else None,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "videoId": vid,
                "thumbnail": thumb,
                "publishedAt": sn.get("publishedAt"),
                "durationSec": seconds,
                "durationText": str(timedelta(seconds=seconds)) if seconds else "",
                "isShorts": is_shorts,
                "description": sn.get("description",""),
            })
        time.sleep(0.12)

    # 기본 정렬
    if st.session_state.yt_sort == "조회수순":
        out.sort(key=lambda x: (x["views"] or -1), reverse=True)
    else:
        out.sort(key=lambda x: x.get("publishedAt","") or "", reverse=True)

    # 연령대 필터 — 블랙리스트 제외 + 해당 연령 키워드 최소 1개 매칭 강제
    if age_tag != "전체":
        out = [r for r in out if not age_negative_hit(r.get("title",""), age_tag)]
        for r in out:
            r["_age_score"] = age_relevance_score(r.get("title",""), age_tag)
        out = [r for r in out if r.get("_age_score", 0) >= 1]
        out.sort(key=lambda x: (x.get("_age_score",0), x.get("views") or 0), reverse=True)

    # dedup
    seen, deduped = set(), []
    for r in out:
        u = r.get("url")
        if u and u not in seen:
            deduped.append(r); seen.add(u)
    return deduped

@st.cache_data(show_spinner=False, ttl=600)
def fetch_trending_with_engagement(region_code: str | None, fetch_total: int, order_mode: str,
                                   age_tag: str = "전체", salt: int = 0):
    if not YOUTUBE_API_KEYS: return []
    per_page, collected, page_token = 50, [], None
    region = region_code or "KR"

    while len(collected) < fetch_total:
        params = {"part":"snippet,contentDetails,statistics","chart":"mostPopular","regionCode":region,"maxResults":per_page}
        if page_token: params["pageToken"] = page_token
        try:
            data = yt_get(YOUTUBE_VIDEOS_URL, params=params)
        except requests.exceptions.HTTPError as e:
            reason = ""
            try:
                err = e.response.json()
                reason = err.get("error", {}).get("errors", [{}])[0].get("reason", "")
            except Exception:
                pass
            st.warning(f"트렌드 API 호출 실패(폴백 사용). reason={reason or 'HTTPError'}")
            break

        items = data.get("items", [])
        if not items: break
        for v in items:
            vid = v["id"]; sn=v.get("snippet",{}); stt=v.get("statistics",{}); cd=v.get("contentDetails",{})
            thumbs = sn.get("thumbnails",{})
            thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url")
            seconds = parse_iso8601_duration(cd.get("duration")); is_shorts = seconds<=60 if seconds else False
            collected.append({
                "platform":"YouTube","title":sn.get("title",""),"author":sn.get("channelTitle",""),
                "views": int(stt.get("viewCount",0)) if stt.get("viewCount") else None,
                "url":f"https://www.youtube.com/watch?v={vid}","videoId":vid,"thumbnail":thumb,
                "publishedAt":sn.get("publishedAt"),"durationSec":seconds,"durationText":str(timedelta(seconds=seconds)) if seconds else "",
                "isShorts":is_shorts,"_eng_score":compute_engagement_score(stt),
                "description": sn.get("description",""),
            })
        page_token = data.get("nextPageToken")
        if not page_token or len(collected) >= fetch_total: break

    if order_mode == "viewCount":
        collected.sort(key=lambda x: (x.get("views") or -1), reverse=True)
    else:
        collected.sort(key=lambda x: x.get("publishedAt") or "", reverse=True)

    if age_tag != "전체":
        collected = [r for r in collected if not age_negative_hit(r.get("title",""), age_tag)]
        for r in collected:
            r["_age_score"] = age_relevance_score(r.get("title",""), age_tag)
        collected = [r for r in collected if r.get("_age_score", 0) >= 1]
        collected.sort(key=lambda x: (x.get("_age_score",0), x.get("_eng_score",0)), reverse=True)

    if not collected: return []
    pool_size = max(40, int(len(collected) * 0.6))
    pool = collected[:pool_size]
    rnd = random.Random(salt or int(time.time()))
    rnd.shuffle(pool)
    return pool[:max(60, min(120, len(pool)))]

# ---------- 폴백 & 키워드별 랭킹 ----------
def build_age_seed_queries(age_tag: str, topk: int = 8) -> list[str]:
    keys = AGE_KEYWORDS.get(age_tag, [])
    ban = set(["건강","여행","요리","가족","취미"])
    qs = [k for k in keys if k not in ban][:topk]
    while len(qs) < topk and len(keys) > len(qs):
        qs.append(keys[len(qs)])
    return qs or ["교양", "뉴스"]

@st.cache_data(show_spinner=False, ttl=600)
def fallback_age_recommendations(age_tag: str, region_code: str, fetch_total_per_q: int = 30) -> list[dict]:
    qs = build_age_seed_queries(age_tag)
    gathered = []
    for q in qs:
        try:
            res = search_youtube(
                q, fetch_total=fetch_total_per_q,
                cc_only=False, upload_window="최근 1년",
                include_channels=[], exclude_channels=[],
                include_channel_ids=[], exclude_channel_ids=[],
                include_words=[], exclude_words=[],
                region_code=region_code, relevance_lang=None,
                safe_mode="moderate", order_mode="viewCount",
                duration_param="any", min_seconds=None, max_seconds=None,
                age_tag=age_tag,
            )
            gathered.extend(res)
        except Exception:
            continue
    seen, dedup = set(), []
    for r in gathered:
        u = r.get("url")
        if u and u not in seen:
            dedup.append(r); seen.add(u)
    dedup.sort(key=lambda x: (x.get("views") or 0), reverse=True)
    return dedup[:200]

@st.cache_data(show_spinner=False, ttl=600)
def keyword_ranked_recos(age_tag: str, region_code: str, per_keyword: int = 6) -> dict:
    keywords = build_age_seed_queries(age_tag, topk=8)
    board = {}
    for kw in keywords:
        try:
            rows = search_youtube(
                kw, fetch_total=120,
                cc_only=False, upload_window="최근 1년",
                include_channels=[], exclude_channels=[],
                include_channel_ids=[], exclude_channel_ids=[],
                include_words=[], exclude_words=[],
                region_code=region_code, relevance_lang=None,
                safe_mode="moderate", order_mode="viewCount",
                duration_param="any", min_seconds=None, max_seconds=None,
                age_tag=age_tag,
            )
            board[kw] = rows[:per_keyword]
        except Exception:
            board[kw] = []
    return board

# =========================
# Tokens & OCR for source trace
# =========================
TOKEN_PATTERNS = [
    re.compile(r"@[\w\.\-]{3,}"),
    re.compile(r"#[0-9]{4,}"),
]

def extract_tokens_from_text(txt: str) -> set[str]:
    toks = set()
    if not txt: return toks
    for pat in TOKEN_PATTERNS:
        toks.update(pat.findall(txt))
    return {t for t in toks if len(t) >= 4}

def ocr_tokens_from_thumb(thumb_url: str) -> set[str]:
    if not HAS_OCR or not thumb_url: return set()
    try:
        data = http_get_bytes(thumb_url, timeout=8)
        img = Image.open(io.BytesIO(data))
        text = pytesseract.image_to_string(img, lang="eng")
        return extract_tokens_from_text(text)
    except Exception:
        return set()

def collect_source_tokens(row: dict, *, try_ocr=True) -> set[str]:
    toks = set()
    toks |= extract_tokens_from_text(row.get("title",""))
    toks |= extract_tokens_from_text(row.get("description",""))
    if try_ocr:
        toks |= ocr_tokens_from_thumb(row.get("thumbnail"))
    return {t for t in toks if len(t) >= 4}

# =========================
# External web search (Google CSE 전용)
# =========================
@st.cache_data(show_spinner=False, ttl=300)
def web_search(query: str, *, num: int = 10):
    results = []
    if CSE_API_KEY and CSE_CX:
        try:
            js = http_get("https://www.googleapis.com/customsearch/v1", {
                "key": CSE_API_KEY, "cx": CSE_CX, "q": query, "num": min(10, num),
                "safe": "off", "hl": "ko"
            })
            for it in js.get("items", []):
                results.append({
                    "title": it.get("title",""),
                    "link": it.get("link",""),
                    "snippet": it.get("snippet","")
                })
        except Exception:
            pass
    return results

def domain_weight(url: str) -> float:
    if not url: return 0
    u = url.lower()
    if "tiktok.com" in u: return 3.0
    if "instagram.com" in u: return 2.5
    if "facebook.com" in u or "fb.watch" in u: return 1.8
    if "x.com" in u or "twitter.com" in u: return 1.6
    if "naver.com" in u or "daum.net" in u: return 1.2
    return 1.0

def extract_keyphrases(text: str, *, topk=5) -> list[str]:
    if not text: return []
    words = re.findall(r"[A-Za-z가-힣0-9]{2,}", text.lower())
    stop = set(["the","and","you","for","with","this","that","are","from","제","것","해서","그리고","하지만","그러나","근데","이건","저건","에서","하다"])
    freq = {}
    for w in words:
        if w in stop: continue
        freq[w] = freq.get(w, 0) + 1
    keys = [w for w,_ in sorted(freq.items(), key=lambda x: x[1], reverse=True)]
    keys = [k for k in keys if len(k) >= 3][:topk]
    return keys

def rank_external_results(tokens: set[str], title: str, results: list[dict], extra_keys: list[str]) -> list[dict]:
    if not results: return []
    toks = {t.lower() for t in tokens}
    keys = {k.lower() for k in extra_keys}
    tlo = (title or "").lower()
    ranked = []
    for r in results:
        b = (r.get("title","") + " " + r.get("snippet","")).lower()
        hit_tok = sum(1 for t in toks if t in b)
        hit_key = sum(1 for k in keys if k in b)
        if hit_tok==0 and hit_key==0:
            continue
        dom = domain_weight(r.get("link",""))
        title_sim = 1.0 if any(w for w in [tlo[:20], *tlo.split()[:3]] if w and w in b) else 0.0
        score = hit_tok*2.0 + hit_key*1.2 + dom + title_sim
        r2 = dict(r)
        r2["_ext_score"] = round(score, 3)
        ranked.append(r2)
    ranked.sort(key=lambda x: x["_ext_score"], reverse=True)
    seen_domain = {}
    filtered = []
    for r in ranked:
        d = re.sub(r"^https?://(www\.)?","", r["link"]).split("/")[0]
        c = seen_domain.get(d, 0)
        if c >= 2: continue
        seen_domain[d] = c+1
        filtered.append(r)
    return filtered[:12]

# =========================
# UI helpers — DARK ONLY
# =========================
def title_bar():
    left, right1, right2, right3 = st.columns([1,0.13,0.13,0.13])
    with left:
        st.markdown("""
<div class="title-rect">
  <div class="title-rect-inner">🛠️ 민석이의 작업실</div>
</div>
""", unsafe_allow_html=True)
        st.caption("원본 링크로만 이동하는 안전한 개인용 검색 도구 (다운로드 기능 없음)")
    btn_html = """
    <div class="title-action-wrap">
      <a href="{href}" target="_blank" class="btn-link">
        {label}
      </a>
    </div>
    """
    with right1:
        st.markdown(btn_html.format(href="https://ssyoutube.online/ko/youtube-video-downloader-ko/", label="유튜브다운"), unsafe_allow_html=True)
    with right2:
        st.markdown(btn_html.format(href="https://snaptik.kim/", label="틱톡다운"), unsafe_allow_html=True)
    with right3:
        st.markdown(btn_html.format(href="https://www.pexels.com/videos/", label="Pexels"), unsafe_allow_html=True)

def thumb_with_badge(url, duration_text, views):
    dur = duration_text or ""
    vtx = fmt_int(views) if views is not None else ""
    html = f"""
<div class="thumb-wrap">
  <img src="{url}" class="thumb-img"/>
  <div class="badge-wrap">
    <span class="badge">{'⏱ ' + dur if dur else ''}</span>
    <span class="badge">{'👀 ' + vtx if vtx else ''}</span>
  </div>
</div>
"""
    st.markdown(html, unsafe_allow_html=True)

def insta_card_wrap(func):
    def inner(*args, **kwargs):
        st.markdown("<div class='card-grid'>", unsafe_allow_html=True)
        func(*args, **kwargs)
        st.markdown("</div>", unsafe_allow_html=True)
    return inner

def page_controls(total_count: int, where: str):
    total_pages = max(1, math.ceil(total_count / st.session_state.page_size))
    l, c, r = st.columns([1,2,1])
    with c:
        nav = st.columns(4, gap="small")
        with nav[0]:
            st.button("⏮ 처음", key=f"{where}_first", on_click=lambda: st.session_state.update(page=1))
        with nav[1]:
            st.button("◀ 이전", key=f"{where}_prev", on_click=lambda: st.session_state.update(page=max(1, st.session_state.page-1)))
        with nav[2]:
            st.button("다음 ▶", key=f"{where}_next", on_click=lambda: st.session_state.update(page=min(total_pages, st.session_state.page+1)))
        with nav[3]:
            st.button("마지막 ⏭", key=f"{where}_last", on_click=lambda: st.session_state.update(page=total_pages))
        st.caption(f"페이지 {st.session_state.page} / {total_pages} · 한 페이지 {st.session_state.page_size}개")

def slice_df_for_page(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty: return df
    ps = st.session_state.page_size; pg = st.session_state.page
    return df.iloc[(pg-1)*ps : (pg-1)*ps + ps]

# =========================
# CSS — 다크모드만 유지
# =========================
def inject_css(accent="#00A389"):
    st.markdown(f"""
<style>
:root {{
  --accent: {accent};
  --grad-start: #ff7ac6; --grad-mid: #ffb86b; --grad-end: #8be9fd;
  --btn-radius: 999px;
}}
.stApp {{
  background: linear-gradient(180deg, #0b0d12 0%, #0f1117 100%);
  color: #eaeef5 !important;
}}
.block-container {{ padding-top: 2.2cm; }}
a {{ color: var(--accent) !important; text-decoration: none; }}
.card-grid {{ margin-top: 4px; }}
div, p, span, label, h1, h2, h3, h4, h5, h6 {{ color:#eaeef5 !important; }}

/* Title */
.title-rect {{ display:inline-block; padding:4px;
  background: linear-gradient(135deg, var(--grad-start), var(--grad-mid), var(--grad-end)); border-radius:10px; }}
.title-rect-inner {{
  padding:8px 14px; background: rgba(12,14,20,0.9); border-radius:8px;
  font-size:1.8rem; font-weight:800; color:#eaeef5;
}}
.title-action-wrap {{ margin-top:18px; text-align:right; }}

/* Buttons */
.btn-link {{
  display:inline-block; padding:8px 12px;
  background: linear-gradient(135deg, var(--grad-start), var(--grad-mid), var(--grad-end));
  color:#fff !important; border-radius:var(--btn-radius); font-weight:700;
  box-shadow:0 4px 14px rgba(0,0,0,0.28); text-align:center; font-size:.82rem;
}}
.btn-link.small {{ padding:8px 12px; font-size:.82rem; }}
.stButton>button {{
  border-radius:var(--btn-radius);
  background: linear-gradient(135deg, var(--grad-start), var(--grad-mid), var(--grad-end));
  border:none; color:white; font-weight:700; font-size:.86rem; padding:9px 14px;
  box-shadow:0 6px 16px rgba(0,0,0,0.28);
}}
.card-grid .stButton>button {{ font-size:.48rem; padding:6px 10px; line-height:1.05; }}
.analysis-back .stButton>button {{ font-size:1.05rem; padding:14px 18px; width:100%;
  box-shadow:0 8px 18px rgba(0,0,0,0.28); }}

/* Thumb badge */
.thumb-wrap {{ position:relative; }}
.thumb-img {{ width:100%; border-radius:16px; display:block; }}
.badge-wrap {{ position:absolute; top:8px; right:8px; display:flex; gap:6px; }}
.badge {{ padding:4px 8px; border-radius:999px; font-weight:800; font-size:.72rem;
  background: rgba(0,0,0,.55); color:#fff; }}

/* Expander */
details {{ border-radius:12px; background:#0f1320; border:1px solid #1f2537; padding:6px 10px; }}
details[open] {{ background:#0c101b; }}

/* Pagination center */
div[data-testid="column"] > div:has(> .stButton) {{ display:flex; justify-content:center; }}

</style>
""", unsafe_allow_html=True)

# =========================
# Renderers
# =========================
@insta_card_wrap
def render_cards(df: pd.DataFrame, *, cols: int, subtitles: list[str], bookmark_key_prefix: str):
    if df is None or df.empty:
        st.info("결과가 없습니다."); return
    rows = math.ceil(len(df)/cols)
    for r in range(rows):
        ccols = st.columns(cols, gap="large")
        for i in range(cols):
            idx = r*cols + i
            if idx >= len(df): break
            row = df.iloc[idx].to_dict()
            with ccols[i]:
                if row.get("thumbnail"):
                    thumb_with_badge(row["thumbnail"], row.get("durationText",""), row.get("views"))
                title = row.get("title", "Untitled"); url = row.get("url", "#")
                st.markdown(f"<div style='font-weight:800; font-size:1.02rem; margin:6px 0 2px'>{title}</div>", unsafe_allow_html=True)

                chips=[]
                for key in subtitles:
                    val=row.get(key)
                    if val in (None,"",0): continue
                    if key=="author": chips.append(f"제작자 {val}")
                    elif key=="views": chips.append(f"조회수 {fmt_int(val)}")
                    elif key=="durationText": chips.append(f"길이 {val}")
                    elif key=="publishedAt": chips.append(f"게시 {str(val)[:10]}")
                if chips: st.caption(" · ".join(chips))

                # ⬇️ 버튼 2개만: 원본링크 / 영상분석 (원본찾기 버튼 제거)
                b1, b2 = st.columns(2, gap="small")
                with b1:
                    st.markdown(f"""<a href="{url}" target="_blank" class="btn-link small" style="display:block;text-align:center;">원본링크</a>""", unsafe_allow_html=True)
                with b2:
                    def _open_analysis(r=row):
                        st.query_params["view"] = "analysis"
                        st.query_params["vid"]  = r.get("videoId","")
                        st.session_state["analysis_target"] = r
                        st.session_state["trigger_analysis"] = True
                        st.rerun()
                    st.button("영상분석", key=f"an_{bookmark_key_prefix}_{idx}_{abs(hash(url))%10_000_000}", use_container_width=True, on_click=_open_analysis)

def render_results(df_all: pd.DataFrame):
    total_rows = len(df_all)
    if total_rows == 0:
        st.info("YouTube 결과가 없습니다."); return
    page_controls(total_rows, where="top")
    df_page = slice_df_for_page(df_all)

    st.subheader("🎬 YouTube")
    tab_all, tab_shorts, tab_video = st.tabs(["전체","쇼츠","영상"])
    with tab_all:
        render_cards(df_page, cols=3, subtitles=["author","views","durationText","publishedAt"], bookmark_key_prefix="yt")
    with tab_shorts:
        df_s = df_page.loc[df_page["isShorts"]==True] if "isShorts" in df_page.columns else df_page.iloc[0:0]
        render_cards(df_s, cols=3, subtitles=["author","views","durationText","publishedAt"], bookmark_key_prefix="yt_s")
    with tab_video:
        df_v = df_page.loc[df_page["isShorts"]==False] if "isShorts" in df_page.columns else df_page.iloc[0:0]
        render_cards(df_v, cols=3, subtitles=["author","views","durationText","publishedAt"], bookmark_key_prefix="yt_v")

    page_controls(total_rows, where="bottom")
    st.markdown("---")

# =========================
# Analysis View
# =========================
def fetch_transcript_any(video_id: str):
    try:
        from youtube_transcript_api import (
            YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound, CouldNotRetrieveTranscript
        )
    except Exception:
        return (None, None)
    try:
        try:
            tr = YouTubeTranscriptApi.get_transcript(video_id, languages=["ko"])
            txt = "\n".join([s.get("text","").strip() for s in tr if s.get("text")])
            if txt.strip(): return (txt, "ko")
        except Exception: pass
        tr = YouTubeTranscriptApi.get_transcript(video_id, languages=["en"])
        txt = "\n".join([s.get("text","").strip() for s in tr if s.get("text")])
        if txt.strip(): return (txt, "en")
    except (TranscriptsDisabled, NoTranscriptFound, CouldNotRetrieveTranscript):
        return (None, None)
    except Exception:
        return (None, None)

def chunk_transcript(transcript: str, max_chars=300) -> list[dict]:
    chunks, buf = [], []
    for line in transcript.splitlines():
        if len(" ".join(buf+[line])) > max_chars:
            chunks.append({"idx": len(chunks)+1, "text": " ".join(buf)}); buf=[line]
        else:
            buf.append(line)
    if buf: chunks.append({"idx": len(chunks)+1, "text":" ".join(buf)})
    return chunks

def heuristic_prompts(title: str, transcript: str | None):
    title_s = (title or "").strip()
    base_kw = []
    if transcript:
        words = re.findall(r"[A-Za-z가-힣0-9]+", transcript.lower())
        stop = set(["그리고","그래서","하지만","그러나","그냥","근데","이건","저건","에서","하다","the","and","to","of","in","a","is"])
        freq = {}
        for w in words:
            if len(w)<=1 or w in stop: continue
            freq[w] = freq.get(w,0)+1
        base_kw = [w for w,_ in sorted(freq.items(), key=lambda x:x[1], reverse=True)[:5]]
    hook = f"{title_s[:40]}? 핵심만 집어서 말할게요." if title_s else "핵심만 집어서 말할게요."
    problem = "사람들이 놓치는 포인트를 짧게 정리해볼까요?"
    solution = f"키워드: {', '.join(base_kw)}" if base_kw else "핵심 키워드를 추리고 메시지를 압축하세요."
    cta = "도움됐다면 저장하고 다음 아이디어로 이어가요."
    shorts_script = f"후킹: {hook}\n문제: {problem}\n해결: {solution}\nCTA: {cta}"
    image_prompt = f'포토리얼, 밝은 톤, 주제: "{title_s}", 핵심어: {", ".join(base_kw) if base_kw else "간결/선명/집중"}'
    return shorts_script, image_prompt

def render_analysis_view(row: dict):
    st.markdown('<div class="analysis-back">', unsafe_allow_html=True)
    if st.button("✖ 닫기(목록으로)", use_container_width=True, key=f"back_{row.get('videoId','')}"):
        st.query_params.clear()
        st.session_state["trigger_analysis"] = False
        st.rerun()
    st.subheader("🧠 영상 분석")

    c1, c2 = st.columns([1,1])
    with c1:
        thumb_with_badge(row.get("thumbnail"), row.get("durationText",""), row.get("views"))
    with c2:
        raw_title = row.get("title",""); ko_title = translate_to_ko(raw_title)
        if ko_title and ko_title != raw_title:
            st.markdown(f"**제목(번역):** {ko_title}"); st.caption(f"원문 제목: {raw_title}")
        else:
            st.markdown(f"**제목:** {raw_title}")
        st.caption(f"채널: {row.get('author','')} · 조회수: {fmt_int(row.get('views')) if row.get('views') is not None else '—'}")
        st.caption(f"게시: {str(row.get('publishedAt') or '')[:10]} · 길이: {row.get('durationText','')}")
        st.markdown(f"""<a href="{row.get("url")}" target="_blank" class="btn-link small">원본 영상 열기</a>""", unsafe_allow_html=True)

        # 👉 분석 내부 ‘원본찾기’ 버튼 (숏츠일 때만 활성)
        is_shorts = bool(row.get("isShorts"))
        col_a1, col_a2 = st.columns(2)
        with col_a1:
            st.caption(" ")
        with col_a2:
            def _open_trace_from_analysis(r=row):
                if not r.get("isShorts"):
                    st.warning("‘원본찾기’는 숏츠에서만 제공됩니다.")
                    return
                st.session_state["analysis_target"] = r
                st.query_params["view"] = "trace"
                st.query_params["vid"]  = r.get("videoId","")
                st.rerun()

            st.button(
                "🧭 원본찾기 (숏츠 전용)",
                key=f"btn_trace_in_analysis_{row.get('videoId','')}",
                disabled=not is_shorts,
                use_container_width=True,
                on_click=_open_trace_from_analysis
            )

    vid = row.get("videoId")
    if not vid:
        st.info("영상 ID를 확인할 수 없어 분석을 진행할 수 없습니다.")
        return

    with st.status("자막 확인/번역 중…", expanded=True) as status:
        st.markdown('<span class="loading-badge"><span class="loading-dot"></span> 처리 중…</span>', unsafe_allow_html=True)
        transcript, lang = fetch_transcript_any(vid)
        status.update(label="완료", state="complete")

    if not transcript:
        st.warning("자막을 가져오지 못했습니다."); return

    show_text = transcript if lang == "ko" else translate_block_to_ko(transcript)
    st.markdown("### 📝 자막 / 타임라인(요약)")
    chunks = chunk_transcript(show_text, max_chars=350)
    for ch in chunks[:12]:
        with st.expander(f"섹션 {ch['idx']}", expanded=False):
            st.write(ch["text"])

    st.markdown("### ✍️ 숏츠 대본 씨앗 & 이미지 프롬프트 (한국어)")
    shorts_script, image_prompt = heuristic_prompts(row.get("title",""), show_text)
    st.text_area("숏츠 대본(후킹/문제/해결/CTA)", shorts_script, height=150, key=f"seed_script_{vid}")
    st.text_area("이미지/영상 프롬프트 시드", image_prompt, height=100, key=f"seed_prompt_{vid}")

# =========================
# Trace View (숏츠 원본찾기)
# =========================
def render_trace_view(row: dict):
    if st.button("✖ 닫기(목록으로)", use_container_width=True, key=f"trace_close_{row.get('videoId','')}"):
        st.query_params.clear()
        st.rerun()

    st.subheader("🧭 원본 소스 추적 (숏츠 → 외부 사이트)")

    # ✅ 수동 모드일 때: 버튼 눌러야 외부 검색(CSE) 시작
    if st.session_state.get("manual_api_mode", True):
        if not st.button("🌐 웹에서 원본 후보 검색 시작", use_container_width=True, key=f"btn_trace_go_{row.get('videoId','')}"):
            st.info("원본 후보 검색을 시작하려면 위 버튼을 눌러주세요.")
            return

    with st.status("출처 토큰 추출 중…", expanded=True) as status:
        tokens = collect_source_tokens(row, try_ocr=True)
        status.update(label="토큰 추출 완료", state="complete")

    title_keys = extract_keyphrases(row.get("title",""), topk=4)
    desc_keys  = extract_keyphrases(row.get("description",""), topk=3)
    key_str = " ".join(title_keys[:2] + desc_keys[:2])

    if not tokens and not key_str:
        st.warning("토큰과 키워드를 추출하지 못했어요. 다른 영상으로 시도해 주세요.")
        return

    st.caption("감지된 토큰: " + (" · ".join(sorted(tokens)) if tokens else "—"))
    st.caption("핵심 키워드: " + (key_str if key_str else "—"))

    token_q = " OR ".join(sorted(tokens))[:180] if tokens else ""
    site_pool = ["tiktok.com","instagram.com","facebook.com","x.com","twitter.com","reddit.com","9gag.com","imgur.com","bilibili.com","tv.naver.com","kakao.tv"]
    all_results = []

    with st.status("웹에서 원본 후보 검색 중…", expanded=True) as status:
        if not (CSE_API_KEY and CSE_CX):
            st.error("외부 웹검색 키가 필요합니다. secrets.toml에 CSE_API_KEY, CSE_CX를 설정해 주세요.")
            return

        for site in site_pool:
            if token_q and key_str:
                q = f"({token_q}) {key_str} site:{site}"
            elif token_q:
                q = f"({token_q}) site:{site}"
            else:
                q = f"{key_str} site:{site}"
            res = web_search(q, num=10)
            all_results.extend(res)
            time.sleep(0.1)

        if token_q and key_str:
            all_results.extend(web_search(f"({token_q}) {key_str}", num=10))
        elif token_q:
            all_results.extend(web_search(f"({token_q})", num=10))
        else:
            all_results.extend(web_search(key_str, num=10))

        status.update(label="후보 수집 완료", state="complete")

    ranked = rank_external_results(tokens, row.get("title",""), all_results, extra_keys=(title_keys+desc_keys))

    if not ranked:
        st.info("후보를 찾지 못했어요. 키워드를 바꿔서 다시 시도해 보세요.")
        return

    st.markdown("#### 후보 리스트")
    for i, cand in enumerate(ranked[:12], 1):
        st.markdown(f"**#{i}. 점수 {cand['_ext_score']}** — {cand.get('title','')}")
        st.caption(cand.get("snippet",""))
        st.markdown(f"""<a href="{cand.get("link")}" target="_blank" class="btn-link small">원본(후보) 열기</a>""", unsafe_allow_html=True)
        st.markdown("---")

# =========================
# APP
# =========================
st.set_page_config(page_title="민석이의 작업실", page_icon="🛠️", layout="wide")
init_state()

# Accent
accent_map = {"기본":"#00A389","블루":"#2F80ED","그린":"#27AE60","핑크":"#EB5757","보라":"#A259FF"}
accent = accent_map.get(st.session_state.accent, "#00A389")

# CSS + Title
inject_css(accent)
title_bar()

# ===== 사이드바 =====
with st.sidebar:
    st.header("검색 옵션")

    # ✅ 수동 호출 모드 토글
    s = st.session_state
    s.manual_api_mode = st.toggle(
        "수동 모드(원할 때만 API 호출)",
        value=s.get("manual_api_mode", True),
        help="켜두면 버튼을 눌렀을 때만 YouTube API를 호출합니다."
    )

    # 최근 검색
    if st.session_state.search_history:
        st.caption("최근 검색")
        hist_cols = st.columns(3)
        for i, qv in enumerate(st.session_state.search_history[-9:][::-1]):
            with hist_cols[i % 3]:
                if st.button(qv, key=f"hist_{i}"):
                    st.session_state["_prefill_query"] = qv
                    st.session_state["sb_query"] = qv
                    # 수동 모드에서는 자동 트리거 안함
                    if not s.manual_api_mode:
                        st.session_state["do_search_now"] = True

    q_default = st.session_state.get("_prefill_query", "prank")

    # 🔧 검색 폼
    with st.form(key="search_form", clear_on_submit=False):
        query = st.text_input("검색 키워드", value=q_default, key="sb_query")
        submit_search = st.form_submit_button("검색", use_container_width=True, type="primary")

    st.markdown("---")
    st.subheader("YouTube 설정")
    yt_cc = st.checkbox("크리에이티브 커먼즈(CC)만", value=False, key="sb_cc")

    st.caption("· 결과 수집량 (많이 올릴수록 느려질 수 있어요)")
    st.session_state.yt_fetch_limit = st.selectbox(
        "YouTube 결과 수집량(최대)",
        options=[50,100,200,300,400,500],
        index=[50,100,200,300,400,500].index(st.session_state.yt_fetch_limit)
              if st.session_state.yt_fetch_limit in [50,100,200,300,400,500] else 1,
        key="sb_fetch_limit"
    )
    st.caption(f"· 한 페이지 보기: **{PAGE_SIZE_FIXED}개 고정**")

    yt_upload_window = st.selectbox(
        "업로드 시점",
        ["전체","최근 24시간","최근 7일","최근 30일","최근 1년"],
        index=0, key="sb_uploadwin"
    )

    region_label = st.selectbox(
        "지역",
        ["자동","한국(KR)","미국(US)","일본(JP)","영국(GB)","독일(DE)","프랑스(FR)","인도(IN)","인도네시아(ID)","브라질(BR)","멕시코(MX)"],
        index=1, key="sb_region"
    )
    region_map = {"자동":"", "한국(KR)":"KR", "미국(US)":"US", "일본(JP)":"JP", "영국(GB)":"GB",
                  "독일(DE)":"DE", "프랑스(FR)":"FR", "인도(IN)":"IN", "인도네시아(ID)":"ID", "브라질(BR)":"BR", "멕시코(MX)":"MX"}
    region_code = region_map[region_label] or "KR"
    st.session_state["region_code"] = region_code

    lang_label = st.selectbox(
        "언어",
        ["자동","한국어(ko)","영어(en)","일본어(ja)","스페인어(es)","프랑스어(fr)","독일어(de)","인도네시아어(id)","포르투갈어(pt)","힌디어(hi)"],
        index=1, key="sb_lang"
    )
    lang_map = {"자동":"", "한국어(ko)":"ko", "영어(en)":"en", "일본어(ja)":"ja", "스페인어(es)":"es", "프랑스어(fr)":"fr", "독일어(de)":"de",
                "인도네시아어(id)":"id", "포르투갈어(pt)":"pt", "힌디어(hi)":"hi"}
    relevance_lang = lang_map[lang_label]

    ylen_label = st.selectbox("길이 필터", ["전체","짧음(<4분)","중간(4~20분)","긴(>20분)"], index=0, key="sb_ylen")
    ylen_map = {"전체":"any","짧음(<4분)":"short","중간(4~20분)":"medium","긴(>20분)":"long"}
    duration_param = ylen_map[ylen_label]

    c1, c2 = st.columns(2)
    with c1:
        min_sec = st.number_input("최소 길이(초)", min_value=0, max_value=86400, value=0, step=5, key="sb_minsec")
        min_seconds = None if min_sec==0 else int(min_sec)
    with c2:
        max_sec = st.number_input("최대 길이(초)", min_value=0, max_value=86400, value=0, step=5, key="sb_maxsec")
        max_seconds = None if max_sec==0 else int(max_sec)

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

    # =========================
    # 🔢 유닛 예상 위젯
    # =========================
    st.markdown("---")
    st.subheader("유닛 사용량(대략) 미리보기")
    est_search = estimate_units_for_youtube_search(st.session_state.yt_fetch_limit)
    est_trend  = estimate_units_for_trending(fetch_total=200)
    est_kw     = estimate_units_for_kwboard(per_keyword=6, keywords=8)
    st.caption("※ search.list는 50개/페이지당 100 units, videos.list는 50개/페이지당 1 unit 기준 추정")
    st.metric("현재 검색 실행 시", f"~ {est_search:,} units")
    st.metric("추천 로드(트렌드 200개)", f"~ {est_trend:,} units")
    st.metric("키워드별 TOP 로드", f"~ {est_kw:,} units")
    if st.session_state.yt_fetch_limit >= 300:
        st.warning("검색 수집량이 큽니다. 유닛 소모가 많이 발생할 수 있어요.")

# ===== 공통: 검색 실행 함수
def perform_search(q: str):
    order_mode = "viewCount" if st.session_state.yt_sort=="조회수순" else "date"
    try:
        yt_all = search_youtube(
            q,
            fetch_total=st.session_state.yt_fetch_limit,
            cc_only=st.session_state.get("sb_cc", False),
            upload_window=st.session_state.get("sb_uploadwin","전체"),
            include_channels=include_channels, exclude_channels=exclude_channels,
            include_channel_ids=include_channel_ids, exclude_channel_ids=exclude_channel_ids,
            include_words=include_words, exclude_words=exclude_words,
            region_code=st.session_state.get("region_code","KR"),
            relevance_lang=(relevance_lang or None),
            safe_mode="moderate", order_mode=order_mode,
            duration_param=duration_param, min_seconds=min_seconds, max_seconds=max_seconds,
            age_tag=st.session_state.get("age_filter","전체"),
        )
        df_all = pd.DataFrame(yt_all)
        st.session_state.results_df = df_all
        st.session_state.last_query = q
        st.session_state.last_params = {
            "region_code": st.session_state.get("region_code","KR"), "relevance_lang": relevance_lang, "duration_param": duration_param,
            "min_seconds": min_seconds, "max_seconds": max_seconds,
            "include_channels": include_channels, "exclude_channels": exclude_channels,
            "include_channel_ids": include_channel_ids, "exclude_channel_ids": exclude_channel_ids,
            "include_words": include_words, "exclude_words": exclude_words,
        }
        st.session_state.page = 1
        return True
    except Exception as e:
        msg = str(e)
        if "quotaExceeded" in msg or "403 Client Error" in msg:
            st.error("YouTube 검색 쿼터가 모두 소진되었어요. YOUTUBE_API_KEY에 여러 키를 넣으면 자동으로 로테이션합니다. (예: 키A,키B,키C)")
        else:
            st.error(f"YouTube 검색 오류: {e}")
        return False

# ===== 상단 툴바 =====
with st.container():
    tb1 = st.columns([1.0])[0]
    with tb1:
        st.session_state.yt_sort = st.radio("YouTube 정렬", ["조회수순","최신순"], horizontal=True,
                                            index=["조회수순","최신순"].index(st.session_state.yt_sort), key="tb_sort")

    # 연령대 빠른 필터 — 클릭 시 필터만 바꾸고, 로드는 버튼으로
    st.caption("연령대 선택 후, 아래 ‘추천 로드 / 키워드 TOP 로드’ 버튼으로 불러오세요.")
    chip_row = st.columns(6)
    for i, a in enumerate(["10대","20대","30대","40대","50대","60대"]):
        with chip_row[i]:
            if st.button(a, key=f"age_{a}"):
                st.session_state.age_filter = a
                st.session_state.results_df = pd.DataFrame()
                st.session_state["reco_clicks"] = 0
                st.query_params.clear()
                # 자동 모드라면 바로 로드 플래그 세팅
                if not st.session_state.get("manual_api_mode", True):
                    st.session_state["want_reco_now"] = True
                st.rerun()

# ===== 추천/키워드 TOP 수동 로드 버튼
if st.session_state.get("manual_api_mode", True):
    col_reco, col_kw = st.columns(2)
    with col_reco:
        if st.button("🎲 추천 로드", use_container_width=True, key="btn_load_reco"):
            st.session_state["want_reco_now"] = True
            st.rerun()
    with col_kw:
        if st.button("🔎 키워드 TOP 로드", use_container_width=True, key="btn_load_kwboard"):
            st.session_state["want_kwboard_now"] = True
            st.rerun()

# =========================
# 랜덤추천 + 키워드별 TOP (버튼으로 진입)
# =========================
if st.session_state.get("want_reco_now", False):
    st.session_state["want_reco_now"] = False
    st.markdown("## 🎲 랜덤추천 (트렌드×참여도)")
    with st.status("추천 목록 로딩 중…", expanded=True) as status:
        st.markdown('<span class="loading-badge"><span class="loading-dot"></span> 불러오는 중</span>', unsafe_allow_html=True)
        order_mode = "viewCount" if st.session_state.yt_sort=="조회수순" else "date"

        reco_candidates = fetch_trending_with_engagement(
            region_code=st.session_state.get("region_code","KR"),
            fetch_total=200,
            order_mode=order_mode,
            age_tag=st.session_state.get("age_filter","전체"),
            salt=st.session_state.get("reco_clicks",0)
        )

        if not reco_candidates or len(reco_candidates) < 12:
            fb = fallback_age_recommendations(
                age_tag=st.session_state.get("age_filter","전체"),
                region_code=st.session_state.get("region_code","KR"),
                fetch_total_per_q=30
            )
            reco_candidates = fb

        status.update(label="완료", state="complete")

    if not reco_candidates:
        st.info("추천 후보를 만들지 못했어요. 키워드를 넓혀서 다시 시도해 주세요.")
    else:
        def _rank_key(x):
            return (x.get("_age_score", 0), x.get("_eng_score", 0), x.get("views") or 0)
        top_sorted = sorted(reco_candidates, key=_rank_key, reverse=True)
        top_n = top_sorted[:12]

        st.subheader("🏆 연령대 TOP 12")
        top_df = pd.DataFrame(top_n)
        render_cards(top_df, cols=3, subtitles=["author","views","durationText","publishedAt"], bookmark_key_prefix="reco_top")

        remain = [r for r in reco_candidates if r not in top_n]
        if remain:
            rnd = random.Random(time.time_ns() ^ st.session_state.get("reco_clicks",0))
            display = rnd.sample(remain, k=min(18, len(remain)))
            st.subheader("🔀 무작위 추천")
            st.caption("※ 연령 필터 + (있다면) 참여도/조회수 상위권의 큰 풀에서 무작위 추출")
            reco_df = pd.DataFrame(display)
            render_cards(reco_df, cols=3, subtitles=["author","views","durationText","publishedAt"], bookmark_key_prefix="reco")
    st.markdown("---")

# ✅ 키워드별 랭킹 보드 (버튼 눌렀을 때만)
if st.session_state.get("want_kwboard_now", False):
    st.session_state["want_kwboard_now"] = False
    st.markdown("## 🔎 키워드별 TOP")
    with st.status("키워드별 상위 영상 수집 중…", expanded=False) as status2:
        board = keyword_ranked_recos(
            age_tag=st.session_state.get("age_filter","전체"),
            region_code=st.session_state.get("region_code","KR"),
            per_keyword=6
        )
        status2.update(label="완료", state="complete")
    for kw, rows in board.items():
        if not rows:
            continue
        st.markdown(f"### #{kw} 상위")
        df_kw = pd.DataFrame(rows)
        render_cards(df_kw, cols=3, subtitles=["author","views","durationText","publishedAt"], bookmark_key_prefix=f"kw_{kw}")
    st.markdown("---")

# =========================
# 검색 실행: (1) 폼 제출, (2) 자동모드 히스토리
# =========================
if submit_search or st.session_state.get("do_search_now", False):
    st.session_state["do_search_now"] = False
    current_q = st.session_state.get("sb_query", "").strip()
    if current_q:
        st.session_state["reco_clicks"] = 0  # 수동 검색 시 추천 섹션 숨김
        if current_q not in st.session_state.search_history:
            st.session_state.search_history.append(current_q)
        if len(st.session_state.search_history) > 10:
            st.session_state.search_history = st.session_state.search_history[-10:]
        with st.status("YouTube 검색 중…", expanded=True) as status:
            st.markdown('<span class="loading-badge"><span class="loading-dot"></span> API 호출 중…</span>', unsafe_allow_html=True)
            ok = perform_search(current_q)
            if ok: status.update(label="완료", state="complete")
            else:  status.update(label="오류", state="error")

# 결과 렌더(페이지 이동 시 재검색 없이 계속 보이게)
if not st.session_state.results_df.empty and st.query_params.get("view","") not in ("analysis","trace"):
    render_results(st.session_state.results_df)

# =========================
# 라우팅: analysis / trace
# =========================
qp_view = st.query_params.get("view","")
if qp_view == "analysis":
    target = st.session_state.get("analysis_target")
    if not target:
        vid = st.query_params.get("vid","")
        if vid and not st.session_state.results_df.empty:
            row = st.session_state.results_df.loc[st.session_state.results_df["videoId"]==vid]
            if not row.empty: target = row.iloc[0].to_dict()
    if target:
        render_analysis_view(target)
    else:
        st.warning("분석 대상을 찾을 수 없습니다. 목록에서 다시 열어주세요.")

elif qp_view == "trace":
    target = st.session_state.get("analysis_target")
    if not target:
        vid = st.query_params.get("vid","")
        if vid and not st.session_state.results_df.empty:
            row = st.session_state.results_df.loc[st.session_state.results_df["videoId"]==vid]
            if not row.empty: target = row.iloc[0].to_dict()
    if target and target.get("isShorts"):
        render_trace_view(target)
    elif target and not target.get("isShorts"):
        st.info("‘원본찾기’는 숏츠에서만 제공됩니다. 쇼츠를 선택해 주세요.")
    else:
        st.warning("대상을 찾을 수 없습니다. 목록에서 다시 시도해 주세요.")

# Footer
st.markdown("<p style='font-variant-caps: all-small-caps; letter-spacing: .03em; opacity: .85;'>본 도구는 원본 링크로만 이동하며 미디어 다운로드 기능을 제공하지 않습니다.</p>", unsafe_allow_html=True)

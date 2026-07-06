# -*- coding: utf-8 -*-
r"""
app.py — KOSPI 종목 분석 애플리케이션 (Streamlit)
로컬 실행:  venv\Scripts\python -m streamlit run app.py
클라우드 배포 시 DART 키는 Secrets(DART_API_KEY)로 자동 인식됩니다.
"""
import os
import re
import io
import math
import datetime
import urllib.parse
import xml.etree.ElementTree as ET
from contextlib import contextmanager
import requests
import pandas as pd
import streamlit as st
import FinanceDataReader as fdr
import plotly.graph_objects as go
from plotly.subplots import make_subplots

try:
    import opendartreader as _odr
except ModuleNotFoundError:
    import OpenDartReader as _odr
DartReader = getattr(_odr, "OpenDartReader", _odr)

CACHE_DIR = "data"
KEY_FILE = "dart_key.txt"
FALLBACK_STOCKS = {
    "005930": "삼성전자", "000660": "SK하이닉스", "066570": "LG전자",
    "009150": "삼성전기", "006400": "삼성SDI", "034220": "LG디스플레이",
    "000990": "DB하이텍",
}
Q2CODE = {1: "11013", 2: "11012", 3: "11014", 4: "11011"}
HIGHER_BETTER = ["유동비율", "자기자본비율", "ROE", "ROA", "영업이익률", "순이익률"]
LOWER_BETTER = ["부채비율", "PER", "PBR", "PSR"]
AX_STAB = ["부채비율", "유동비율", "자기자본비율"]
AX_PROF = ["ROE", "ROA", "영업이익률", "순이익률"]
AX_VAL = ["PER", "PBR", "PSR"]
SCORE_COLS = ["매력도", "안정성", "수익성", "밸류에이션"]
DETAIL_COLS = ["현재가", "시총(억)", "부채비율", "유동비율", "자기자본비율",
               "ROE", "ROA", "영업이익률", "순이익률", "PER", "PBR", "PSR"]
TF_RULE = {"일봉": None, "주봉": "W", "월봉": "ME"}
TF_TAIL = {"일봉": 180, "주봉": 150, "월봉": 120}


# ---------- 유틸 ----------
@contextmanager
def guard(label="이 부분"):
    """어떤 섹션에서 오류가 나도 그 칸만 건너뛰고 앱 전체는 계속 동작하게 한다."""
    try:
        yield
    except Exception as e:
        # Streamlit 흐름 제어(st.stop/st.rerun)는 그대로 통과시킴
        if type(e).__name__ in ("StopException", "RerunException", "RerunData"):
            raise
        st.warning(f"⚠️ {label}을(를) 표시하는 중 문제가 생겨 이 부분만 건너뛰었어요. "
                   "다른 기능은 정상 이용할 수 있어요.")
        with st.expander("자세한 원인 보기 (개발용)"):
            st.exception(e)


def safe_gradient(styler, subset):
    """matplotlib 없으면 색칠만 건너뛰고 표는 그대로 표시(앱이 죽지 않게)."""
    try:
        return styler.background_gradient(subset=subset, cmap="Greens")
    except Exception:
        return styler


def to_num(x):
    if x is None:
        return float("nan")
    s = str(x).replace(",", "").strip()
    if s in ("", "-", "nan", "None"):
        return float("nan")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def safe_div(a, b):
    if pd.isna(a) or pd.isna(b) or b == 0:
        return float("nan")
    return a / b


def clean_sector(s):
    s = ("" if s is None else str(s)).strip()
    if s.lower() in ("nan", "none", ""):
        return "(기타)"
    return s


def pick_amount(df, sj_div, names):
    sub = df[df["sj_div"] == sj_div]
    for fs in ("CFS", "OFS"):
        s2 = sub[sub["fs_div"] == fs]
        for nm in names:
            row = s2[s2["account_nm"] == nm]
            if len(row) > 0:
                v = to_num(row.iloc[0]["thstrm_amount"])
                if pd.notna(v):
                    return v
    return float("nan")


def read_api_key():
    # 1) 클라우드 배포: Streamlit Secrets
    try:
        if "DART_API_KEY" in st.secrets:
            return str(st.secrets["DART_API_KEY"]).strip()
    except Exception:
        pass
    # 2) 로컬: dart_key.txt
    if os.path.exists(KEY_FILE):
        k = open(KEY_FILE, encoding="utf-8").read().strip()
        if k:
            return k
    return ""


def save_api_key(k):
    with open(KEY_FILE, "w", encoding="utf-8") as f:
        f.write(k.strip())


def period_options():
    """오늘 날짜 기준, 공시됐을 최신 분기를 맨 앞(가장 최근)에 두고 과거순으로 나열."""
    today = datetime.date.today()
    y, md = today.year, (today.month, today.day)
    # 대략의 정기공시 마감: 1분기 5/15, 반기 8/14, 3분기 11/14
    if md >= (11, 14):
        latest_q = 3
    elif md >= (8, 14):
        latest_q = 2
    elif md >= (5, 15):
        latest_q = 1
    else:
        latest_q = 0  # 올해 분기 공시 전 → 전년 자료부터
    opts = {}
    for q in range(latest_q, 0, -1):          # 올해: 최신분기 → 1분기
        opts[f"{y}년 {q}분기"] = (y, Q2CODE[q])
    for yy in range(y - 1, y - 6, -1):        # 과거 5개년: 4~1분기
        for q in (4, 3, 2, 1):
            opts[f"{yy}년 {q}분기"] = (yy, Q2CODE[q])
    return opts


# ---------- 데이터 로딩 (캐시) ----------
@st.cache_resource(show_spinner=False)
def get_dart(api_key):
    return DartReader(api_key)


@st.cache_data(show_spinner=False)
def get_naver_industry_map():
    base = "https://finance.naver.com"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(base + "/sise/sise_group.naver?type=upjong", headers=headers, timeout=10)
        r.encoding = "euc-kr"
        items = re.findall(
            r'href="/sise/sise_group_detail\.naver\?type=upjong&(?:amp;)?no=(\d+)"[^>]*>([^<]+)</a>',
            r.text)
        code_map = {}
        for no, sector in items:
            sector = sector.strip()
            if not sector:
                continue
            try:
                dr = requests.get(base + f"/sise/sise_group_detail.naver?type=upjong&no={no}",
                                  headers=headers, timeout=10)
                dr.encoding = "euc-kr"
                for code in re.findall(r'/item/main\.naver\?code=(\d{6})', dr.text):
                    code_map[code] = sector
            except Exception:
                continue
        return code_map
    except Exception:
        return {}


@st.cache_data(show_spinner=False)
@st.cache_data(show_spinner=False)
def get_news(name, n=8):
    """구글 뉴스 RSS에서 검색어가 포함된 최근 기사 (제목, 링크). 실패 시 []."""
    q = urllib.parse.quote(name)
    url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        root = ET.fromstring(r.content)
        out = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if title and link:
                out.append((title, link))
            if len(out) >= n:
                break
        return out
    except Exception:
        return []


@st.cache_data(show_spinner=False)
def get_kospi_listing():
    base = fdr.StockListing("KOSPI")
    cols = base.columns
    code_col = "Code" if "Code" in cols else ("Symbol" if "Symbol" in cols else cols[0])
    name_col = "Name" if "Name" in cols else code_col
    out = pd.DataFrame()
    out["코드"] = base[code_col].astype(str).str.zfill(6)
    out["이름"] = base[name_col].astype(str)
    out["시가총액"] = pd.to_numeric(base["Marcap"], errors="coerce") if "Marcap" in cols else float("nan")
    out["현재가"] = pd.to_numeric(base["Close"], errors="coerce") if "Close" in cols else float("nan")
    out["업종"] = "(기타)"
    try:
        desc = fdr.StockListing("KRX-DESC")
        dcols = desc.columns
        dcode = "Code" if "Code" in dcols else ("Symbol" if "Symbol" in dcols else dcols[0])
        sector_col = next((c for c in ["Sector", "Industry", "업종"] if c in dcols), None)
        if sector_col:
            smap = {c: clean_sector(s) for c, s in
                    zip(desc[dcode].astype(str).str.zfill(6), desc[sector_col])}
            out["업종"] = out["코드"].map(smap).fillna("(기타)")
    except Exception:
        pass
    naver_map = get_naver_industry_map()
    if naver_map:
        out["업종"] = out["코드"].map(naver_map).fillna(out["업종"])
    return out


@st.cache_data(show_spinner=False)
def get_financials(code, year, reprt_code, api_key):
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, f"finstate_{code}_{year}_{reprt_code}.csv")
    if os.path.exists(cache_path):
        df = pd.read_csv(cache_path, dtype=str)
    else:
        try:
            df = get_dart(api_key).finstate(code, year, reprt_code=reprt_code)
        except Exception:
            return None
        if df is None or len(df) == 0:
            return None
        df.to_csv(cache_path, index=False, encoding="utf-8-sig")
    return {
        "자산총계": pick_amount(df, "BS", ["자산총계"]),
        "부채총계": pick_amount(df, "BS", ["부채총계"]),
        "자본총계": pick_amount(df, "BS", ["자본총계"]),
        "유동자산": pick_amount(df, "BS", ["유동자산"]),
        "유동부채": pick_amount(df, "BS", ["유동부채"]),
        "매출액": pick_amount(df, "IS", ["매출액", "수익(매출액)"]),
        "영업이익": pick_amount(df, "IS", ["영업이익"]),
        "당기순이익": pick_amount(df, "IS", ["당기순이익", "당기순이익(손실)"]),
    }


@st.cache_data(show_spinner=False)
def get_price(code, days=2000):
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    try:
        df = fdr.DataReader(code, start, end)
    except Exception:
        return None
    return df if df is not None and len(df) else None


# ---------- 분석 ----------
@st.cache_data(show_spinner=False)
def annual_op_margin(code, api_key):
    """가장 최근 '연간(사업보고서)' 영업이익률(%)과 그 연도. 분기 변동을 배제한 안정적 수치."""
    this_year = datetime.date.today().year
    for y in (this_year - 1, this_year - 2, this_year - 3):
        f = get_financials(code, y, "11011", api_key)
        if f and pd.notna(f.get("영업이익")) and pd.notna(f.get("매출액")) and f["매출액"]:
            return f["영업이익"] / f["매출액"] * 100, y
    return None, None


def build_metrics(stocks, year, reprt_code, api_key, marcap_map, price_map, progress=None):
    rows = []
    items = list(stocks.items())
    for i, (code, name) in enumerate(items):
        if progress:
            progress.progress((i + 1) / len(items), text=f"재무 수집 {i+1}/{len(items)}: {name}")
        f = get_financials(code, year, reprt_code, api_key)
        if not f:
            continue
        asset, debt, eq = f["자산총계"], f["부채총계"], f["자본총계"]
        sales, op, net = f["매출액"], f["영업이익"], f["당기순이익"]
        mc = marcap_map.get(code, float("nan"))
        rows.append({
            "이름": name, "코드": code,
            "현재가": price_map.get(code, float("nan")),
            "시총(억)": (mc / 1e8) if pd.notna(mc) else float("nan"),
            "부채비율": safe_div(debt, eq) * 100,
            "유동비율": safe_div(f["유동자산"], f["유동부채"]) * 100,
            "자기자본비율": safe_div(eq, asset) * 100,
            "ROE": safe_div(net, eq) * 100,
            "ROA": safe_div(net, asset) * 100,
            "영업이익률": safe_div(op, sales) * 100,
            "순이익률": safe_div(net, sales) * 100,
            "PER": safe_div(mc, net) if (net and net > 0) else float("nan"),
            "PBR": safe_div(mc, eq),
            "PSR": safe_div(mc, sales),
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("이름")


def _phi(z):
    """표준정규 누적분포(0~1). z가 클수록 1에 가까움."""
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def zscore_pct(series, reverse=False):
    """업종 내 표준화(z) 후 정규 CDF로 0~100 환산. 값의 크기 차이까지 반영.
    - winsorize(±3σ)로 이상치 왜곡 방지
    - reverse=True 는 낮을수록 유리한 지표(부채비율·PER·PBR·PSR)"""
    s = pd.to_numeric(series, errors="coerce")
    mu = s.mean()
    sd = s.std(ddof=0)
    if pd.isna(sd) or sd == 0:
        return pd.Series([50.0 if pd.notna(v) else float("nan") for v in s], index=s.index)
    z = ((s - mu) / sd).clip(-3, 3)
    if reverse:
        z = -z
    return z.apply(lambda v: round(_phi(v) * 100, 1) if pd.notna(v) else float("nan"))


def score_and_rank(metrics, w_s, w_p, w_v):
    sc = pd.DataFrame(index=metrics.index)
    for c in HIGHER_BETTER:
        sc[c] = zscore_pct(metrics[c], reverse=False)
    for c in LOWER_BETTER:
        sc[c] = zscore_pct(metrics[c], reverse=True)
    안정성 = sc[AX_STAB].mean(axis=1)
    수익성 = sc[AX_PROF].mean(axis=1)
    밸류 = sc[AX_VAL].mean(axis=1)
    if 밸류.notna().any():
        밸류 = 밸류.fillna(밸류.mean())
        tot = (w_s + w_p + w_v) or 1
        매력도 = (w_s * 안정성 + w_p * 수익성 + w_v * 밸류) / tot
    else:
        tot = (w_s + w_p) or 1
        매력도 = (w_s * 안정성 + w_p * 수익성) / tot
    result = metrics.copy()
    result["안정성"] = 안정성.round(1)
    result["수익성"] = 수익성.round(1)
    result["밸류에이션"] = 밸류.round(1)
    result["매력도"] = 매력도.round(1)
    result = result.sort_values("매력도", ascending=False)
    sub = sc.copy()
    sub["안정성"] = 안정성.round(1)
    sub["수익성"] = 수익성.round(1)
    sub["밸류에이션"] = 밸류.round(1)
    return result, sub.reindex(result.index)


def make_ohlc(df, tf):
    rule = TF_RULE[tf]
    if rule is None:
        o = df.copy()
    else:
        o = df.resample(rule).agg({"Open": "first", "High": "max", "Low": "min",
                                   "Close": "last", "Volume": "sum"}).dropna(subset=["Close"])
    o["MA5"] = o["Close"].rolling(5).mean()
    o["MA20"] = o["Close"].rolling(20).mean()
    delta = o["Close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    o["RSI"] = 100 - 100 / (1 + gain / loss)
    return o


def price_compare_table(df):
    def fmt_date(d):
        try:
            return pd.Timestamp(d).strftime("%Y-%m-%d")
        except Exception:
            return "-"

    def fmt_won(v):
        return f"{v:,.0f}원" if pd.notna(v) else "-"

    def fmt_pct(v):
        return f"{v:+.1f}%" if pd.notna(v) else "—"

    last_date = df.index[-1]
    cur = df["Close"].iloc[-1]
    rows = [("현재", fmt_date(last_date), fmt_won(cur), "—")]
    for label, yrs in [("3년 전", 3), ("5년 전", 5)]:
        try:
            tgt = last_date - pd.DateOffset(years=yrs)
            s = df[df.index <= tgt]
            if len(s):
                past = s["Close"].iloc[-1]
                rows.append((label, fmt_date(s.index[-1]), fmt_won(past),
                             fmt_pct((cur / past - 1) * 100)))
            else:
                rows.append((label, "-", "-", "—"))
        except Exception:
            rows.append((label, "-", "-", "—"))
    return pd.DataFrame(rows, columns=["구분", "날짜", "종가", "현재가 대비"]).set_index("구분")


# ---------- 시장지표 · 리포트 ----------
INDICATORS = {
    "국내 증시": [("KOSPI", "KS11"), ("KOSDAQ", "KQ11")],
    "해외 증시": [("S&P500", "US500"), ("나스닥", "IXIC"), ("다우", "DJI")],
    "환율": [("달러/원", "USD/KRW"), ("엔/원", "JPY/KRW"), ("유로/원", "EUR/KRW")],
    "금리": [("미국 10년 국채(%)", "FRED:DGS10"), ("미국 기준금리(%)", "FRED:DFF")],
    "유가·금": [("WTI 유가($)", "FRED:DCOILWTICO"), ("금($/oz)", "FRED:GOLDAMGBD228NLBM")],
}


@st.cache_data(show_spinner=False, ttl=1800)
def fetch_indicator(ticker):
    """최근 종가와 전일대비 변화율. 실패 시 None."""
    try:
        start = datetime.date.today() - datetime.timedelta(days=30)
        df = fdr.DataReader(ticker, start)
        if df is None or len(df) == 0:
            return None
        col = "Close" if "Close" in df.columns else df.columns[0]
        s = pd.to_numeric(df[col], errors="coerce").dropna()
        if len(s) == 0:
            return None
        last = float(s.iloc[-1])
        prev = float(s.iloc[-2]) if len(s) >= 2 else last
        chg = (last / prev - 1) * 100 if prev else 0.0
        return last, chg
    except Exception:
        return None


def render_reports(name, code):
    """기업명·종목코드 기준 증권사 리포트·리서치 바로가기."""
    q = urllib.parse.quote(name)
    naver = f"https://finance.naver.com/research/company_list.naver?searchType=itemCode&itemCode={code}&itemName={q}"
    hankyung = "https://consensus.hankyung.com/analysis/list?search_text=" + q
    google = "https://www.google.com/search?q=" + urllib.parse.quote(f"{name} 증권사 리포트 목표주가")
    st.markdown(
        f"📑 [네이버 금융 종목 리서치]({naver})  ·  "
        f"[한경 컨센서스(증권사 리포트)]({hankyung})  ·  "
        f"[구글에서 '{name}' 리포트 검색]({google})")
    st.caption("증권사 애널리스트 리포트·목표주가·리서치를 위 링크에서 바로 확인할 수 있어요.")


@st.cache_data(show_spinner=False, ttl=1800)
def get_investor_table_naver(code, rows=20):
    """네이버 '외국인·기관 순매매 거래량' 표를 그대로 가져옴. 실패 시 None."""
    url = f"https://finance.naver.com/item/frgn.naver?code={code}"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        r.encoding = "euc-kr"
        tables = pd.read_html(io.StringIO(r.text))
    except Exception:
        return None

    def flat(col):
        if isinstance(col, tuple):
            seen = []
            for p in col:
                s = str(p).strip()
                if s and "Unnamed" not in s and s not in seen:
                    seen.append(s)
            return " ".join(seen)
        return str(col).strip()

    for t in tables:
        cols = [flat(c) for c in t.columns]
        if any("날짜" in c for c in cols) and any("거래량" in c for c in cols):
            df = t.copy()
            df.columns = cols

            def find(key):
                return next((c for c in cols if key in c), None)

            wanted = [("날짜", "날짜"), ("종가", "종가"), ("전일비", "전일비"),
                      ("등락률", "등락률"), ("거래량", "거래량"),
                      ("기관", "기관 순매매량"), ("외국인 순매매량", "외국인 순매매량"),
                      ("보유주수", "외국인 보유주수"), ("보유율", "외국인 보유율")]
            rename = {}
            for key, newname in wanted:
                c = find(key)
                if c and c not in rename:
                    rename[c] = newname
            if "날짜" not in rename.values():
                return None
            df = df[list(rename.keys())].rename(columns=rename)
            date_col = "날짜"
            df = df[df[date_col].notna()].dropna(how="all").head(rows).reset_index(drop=True)
            return df if len(df) else None
    return None


def _fmt_num(v):
    """천단위 콤마 (부호 없음)."""
    if pd.isna(v):
        return "-"
    try:
        return f"{int(round(float(v))):,}"
    except (ValueError, TypeError):
        return str(v)


def _fmt_signed(v):
    """자체 부호 값에 +/- 와 콤마."""
    if pd.isna(v):
        return "-"
    try:
        n = int(round(float(v)))
    except (ValueError, TypeError):
        return str(v)
    return f"{n:+,}" if n != 0 else "0"


def _rate_sign(x):
    """등락률 문자열에서 방향(+1/-1/0) 추출."""
    s = str(x).strip()
    if s.startswith("-"):
        return -1
    num = re.sub(r"[^0-9.]", "", s)
    try:
        return 1 if float(num) > 0 else 0
    except ValueError:
        return 0


def _fmt_change(v, sign):
    """전일비 크기에 방향 부호 + 콤마."""
    if pd.isna(v):
        return "-"
    try:
        n = abs(int(round(float(v))))
    except (ValueError, TypeError):
        return str(v)
    if sign > 0:
        return f"+{n:,}"
    if sign < 0:
        return f"-{n:,}"
    return f"{n:,}"


def _fmt_rate(x):
    """등락률에 +/- 부호 보장 (%는 유지)."""
    s = str(x).strip()
    if s in ("", "nan", "None"):
        return "-"
    if s.startswith("-") or s.startswith("+"):
        return s
    num = re.sub(r"[^0-9.]", "", s)
    try:
        return ("+" + s) if float(num) > 0 else s
    except ValueError:
        return s


def render_investor(code, name):
    st.markdown("#### 📦 외국인 · 기관 순매매 · 거래량")
    df = get_investor_table_naver(code)
    if df is not None and not df.empty:
        disp = df.copy()
        signs = disp["등락률"].map(_rate_sign) if "등락률" in disp.columns else None
        if "전일비" in disp.columns and signs is not None:
            disp["전일비"] = [_fmt_change(v, s) for v, s in zip(disp["전일비"], signs)]
        if "등락률" in disp.columns:
            disp["등락률"] = disp["등락률"].map(_fmt_rate)
        for c in ["기관 순매매량", "외국인 순매매량"]:
            if c in disp.columns:
                disp[c] = disp[c].map(_fmt_signed)
        for c in ["종가", "거래량", "외국인 보유주수"]:
            if c in disp.columns:
                disp[c] = disp[c].map(_fmt_num)
        st.caption("순매매량: +순매수 / −순매도 · 단위: 주 · 출처: 네이버 금융")
        st.dataframe(disp, use_container_width=True, hide_index=True)
    else:
        st.caption("표 데이터를 불러오지 못했어요. 아래 링크에서 바로 확인할 수 있어요.")
    naver = f"https://finance.naver.com/item/frgn.naver?code={code}"
    st.markdown(f"🔗 [네이버 금융에서 '{name}' 매매동향 원본 보기]({naver})")


def render_market():
    st.subheader("🌐 주요 시장지표")
    st.caption("국내외 증시·환율·금리·유가·금 시세 (약 30분 캐시). "
               "일부 지표는 제공처 사정으로 지연·누락될 수 있어요.")
    for group, items in INDICATORS.items():
        st.markdown(f"#### {group}")
        cols = st.columns(len(items))
        for col, (label, ticker) in zip(cols, items):
            r = fetch_indicator(ticker)
            if r is None:
                col.metric(label, "-")
            else:
                last, chg = r
                col.metric(label, f"{last:,.2f}", f"{chg:+.2f}%")
    st.markdown("#### 📰 주요 뉴스")
    render_news("증시 시황")


# ---------- 탭 렌더 ----------
def render_news(name):
    """기업명 기준 네이버·구글 뉴스 바로가기 + 구글 뉴스 헤드라인."""
    q = urllib.parse.quote(name)
    naver = "https://search.naver.com/search.naver?where=news&query=" + q
    google = "https://news.google.com/search?q=" + q + "&hl=ko&gl=KR&ceid=KR:ko"
    st.markdown(f"🔎 [네이버 뉴스에서 '{name}' 보기]({naver})  ·  [구글 뉴스에서 '{name}' 보기]({google})")
    news = get_news(name)
    if news:
        for title, link in news:
            st.markdown(f"- [{title}]({link})")
    else:
        st.caption("헤드라인을 불러오지 못했어요. 위 검색 링크로 바로 확인할 수 있어요.")


SCORE_UNIT = {"부채비율": "%", "유동비율": "%", "자기자본비율": "%", "ROE": "%", "ROA": "%",
              "영업이익률": "%", "순이익률": "%", "PER": "배", "PBR": "배", "PSR": "배"}


def _grade(score):
    if pd.isna(score):
        return "자료 없음"
    if score >= 80:
        return "업종 최상위권"
    if score >= 60:
        return "업종 상위권"
    if score >= 40:
        return "업종 중간"
    if score >= 20:
        return "업종 하위권"
    return "업종 최하위권"


IND_DESC = {
    "부채비율": "자기자본 대비 총부채. 낮을수록 빚 부담이 적어 재무가 튼튼",
    "유동비율": "유동부채 대비 유동자산. 높을수록 단기 지급능력이 좋음",
    "자기자본비율": "총자산 중 자기자본 비중. 높을수록 자본이 자산을 탄탄히 받침",
    "ROE": "자기자본이익률. 주주 자본으로 낸 이익률, 높을수록 수익성 우수",
    "ROA": "총자산이익률. 자산을 얼마나 효율적으로 이익화했는지, 높을수록 우수",
    "영업이익률": "매출 대비 영업이익. 본업의 수익성, 높을수록 우수",
    "순이익률": "매출 대비 순이익. 최종적으로 남는 이익률, 높을수록 우수",
    "PER": "주가 ÷ 주당순이익. 낮을수록 이익 대비 주가가 저렴",
    "PBR": "주가 ÷ 주당순자산. 낮을수록 자산 대비 주가가 저렴",
    "PSR": "주가 ÷ 주당매출. 낮을수록 매출 대비 주가가 저렴",
}
AXIS_DESC = {
    "안정성": "빚 부담과 지급능력 등 '재무가 얼마나 튼튼한가'",
    "수익성": "자본·자산·매출 대비 '얼마나 잘 버는가'",
    "밸류에이션": "실적·자산 대비 '주가가 싼가 비싼가'",
}


AXIS_TRAIT = {
    "안정성": {"high": "재무구조가 매우 탄탄합니다(빚 부담이 적음).",
               "mid": "재무 안정성은 업종 평균 수준입니다.",
               "low": "부채 부담 등 재무 안정성이 약한 편입니다."},
    "수익성": {"high": "업종 내에서 잘 버는, 수익성이 우수한 기업입니다.",
               "mid": "수익성은 업종 평균 수준입니다.",
               "low": "수익성이 업종 대비 부진합니다."},
    "밸류에이션": {"high": "실적·자산 대비 주가가 저렴한(싼) 편입니다.",
                   "mid": "주가는 업종 평균 수준입니다.",
                   "low": "실적·자산 대비 주가가 다소 비싼 편입니다."},
}


def _band(score):
    if pd.isna(score):
        return "mid"
    if score >= 60:
        return "high"
    if score < 40:
        return "low"
    return "mid"


def _characterize(bands):
    s, p, v = bands["안정성"], bands["수익성"], bands["밸류에이션"]
    highs = [a for a in ("안정성", "수익성", "밸류에이션") if bands[a] == "high"]
    if s == "high" and p == "high" and v == "high":
        return "안정성·수익성·밸류에이션 삼박자를 고루 갖춘 **우량주** 성격입니다."
    if p == "high" and v == "low":
        return "잘 벌지만 주가는 비싼 **성장·프리미엄형**입니다."
    if s == "high" and v == "high":
        return "재무가 탄탄하면서 주가도 부담 없는 **안정·가치형**입니다."
    if v == "high" and p in ("low", "mid"):
        return "주가는 저평가된 **가치형**이나, 수익성 개선 여부를 함께 볼 필요가 있습니다."
    if p == "high":
        return "수익성이 강점인 **실적형**입니다."
    if s == "high":
        return "재무 안정성이 돋보이는 **안정형**입니다."
    if not highs:
        return "뚜렷한 강점이 적어 업종 내에서 평범하거나 다소 약한 편입니다."
    return f"**{highs[0]}**이(가) 상대적 강점입니다."


def render_basis(name, r_raw, r_sc, rank, n):
    """선택 종목의 안정성·수익성·밸류에이션 점수 근거를 판단기준과 함께 축별로 설명 + 총평."""
    st.markdown(f"**{name}** 의 점수 근거 — 각 지표를 업종 내에서 표준화(z점수)해 "
                "0~100으로 환산하고, 축별로 종합합니다. (같은 지표끼리 비교, 100=업종 최상위)")
    for axis, inds in [("안정성", AX_STAB), ("수익성", AX_PROF), ("밸류에이션", AX_VAL)]:
        ax_score = r_sc[axis]
        st.markdown(f"##### {axis} {ax_score:.1f}점 — {_grade(ax_score)}")
        st.caption(f"{axis}은(는) {AXIS_DESC[axis]}를 봅니다.")
        for ind in inds:
            raw, sc = r_raw.get(ind, float("nan")), r_sc.get(ind, float("nan"))
            unit = SCORE_UNIT.get(ind, "")
            better = "낮을수록 유리" if ind in LOWER_BETTER else "높을수록 유리"
            raw_txt = f"{raw:,.1f}{unit}" if pd.notna(raw) else "-"
            sc_txt = f"{sc:.0f}점" if pd.notna(sc) else "-"
            if pd.notna(sc):
                contrib = (f"{axis} 점수를 끌어올림" if sc >= 60 else
                           f"{axis} 점수를 끌어내림" if sc <= 40 else f"{axis}에 중립적")
            else:
                contrib = "자료 없음"
            st.markdown(
                f"- **{ind} {raw_txt}** — {IND_DESC[ind]}({better}). "
                f"업종 내 위치 **{sc_txt}**({_grade(sc)}) → *{contrib}*")
        st.caption(f"→ 위 지표들의 표준화 점수를 평균해 **{axis} {ax_score:.1f}점**으로 종합했습니다.")

    scores = {"안정성": r_sc["안정성"], "수익성": r_sc["수익성"], "밸류에이션": r_sc["밸류에이션"]}
    bands = {ax: _band(scores[ax]) for ax in scores}
    st.markdown(f"**📝 총평 — {name} (업종 {n}종목 중 매력도 {rank}위)**")
    for axis in ("안정성", "수익성", "밸류에이션"):
        st.markdown(f"- **{axis} {scores[axis]:.0f}점**({_grade(scores[axis])}): "
                    f"{AXIS_TRAIT[axis][bands[axis]]}")
    st.markdown(f"**👉 한눈에:** {_characterize(bands)}")
    st.caption("점수가 높을수록 그 축에서 업종 내 우위에 있다는 뜻이며, "
               "부채비율·PER·PBR·PSR은 값이 낮을수록 높은 점수로 환산됩니다. "
               "적용 지표 — 안정성: 부채비율·유동비율·자기자본비율 / "
               "수익성: ROE·ROA·영업이익률·순이익률 / 밸류에이션: PER·PBR·PSR")


def tab_summary(result, focus, sector=None, allow_pick=True):
    names = result.index.tolist()
    if allow_pick and len(names) > 1:
        idx = names.index(focus) if focus in names else 0
        name = st.selectbox("요약할 종목 선택", names, index=idx, key="summary_pick")
    else:
        name = focus if focus in names else names[0]
        if focus not in names:
            st.info(f"'{focus}'의 재무 데이터가 없어 같은 업종의 다른 종목을 표시합니다.")
    r = result.loc[name]
    rank = result.index.get_loc(name) + 1

    def q(v, suf=""):
        return f"{v:,.1f}{suf}" if pd.notna(v) else "-"

    st.subheader(f"📋 {name} — 분석 요약")

    # 기본 정보 3가지
    b1, b2, b3 = st.columns(3)
    b1.metric("현재가", f"{r['현재가']:,.0f}원" if pd.notna(r["현재가"]) else "-")
    b2.metric("시가총액", f"{r['시총(억)']:,.0f}억원" if pd.notna(r["시총(억)"]) else "-")
    b3.metric("업종 내 순위", f"{rank}위 / {len(result)}종목")

    # 매력도 = 세 축의 가중 평균 (직관적 표현)
    tot = (w_s + w_p + w_v) or 1
    ps, pp, pv = w_s / tot * 100, w_p / tot * 100, w_v / tot * 100
    st.markdown(f"### 🎯 매력도 {r['매력도']:.1f}")
    st.caption("매력도는 아래 세 축(안정성·수익성·밸류에이션) 점수를 가중 평균한 결과입니다.")
    a, b, c = st.columns(3)
    a.metric(f"안정성 · 비중 {ps:.0f}%", f"{r['안정성']:.1f}")
    b.metric(f"수익성 · 비중 {pp:.0f}%", f"{r['수익성']:.1f}")
    c.metric(f"밸류에이션 · 비중 {pv:.0f}%", f"{r['밸류에이션']:.1f}")
    st.markdown(
        f"**산정식:** 안정성 {r['안정성']:.1f}×{ps:.0f}% + 수익성 {r['수익성']:.1f}×{pp:.0f}% "
        f"+ 밸류에이션 {r['밸류에이션']:.1f}×{pv:.0f}% = **매력도 {r['매력도']:.1f}**")

    # 핵심 지표 — 기업 vs 업종 평균
    st.markdown("#### 📊 핵심 지표 — 기업 vs 업종 평균")

    def cmp(colw, label, val, avg, inverse):
        if pd.isna(val):
            colw.metric(label, "-")
            return
        avg_txt = f"{avg:.1f}" if pd.notna(avg) else "-"
        diff = (val - avg) if pd.notna(avg) else 0.0
        colw.metric(label, f"{val:.1f}", f"업종평균 {avg_txt} ({diff:+.1f})",
                    delta_color=("inverse" if inverse else "normal"))

    per_a, pbr_a, roe_a = result["PER"].mean(), result["PBR"].mean(), result["ROE"].mean()
    p1, p2, p3 = st.columns(3)
    cmp(p1, "PER", r["PER"], per_a, inverse=True)
    cmp(p2, "PBR", r["PBR"], pbr_a, inverse=True)
    cmp(p3, "ROE(%)", r["ROE"], roe_a, inverse=False)

    # 업종 대비 주가 적정성 — 종합 판단
    st.markdown("#### 🧭 업종 대비 주가 적정성 (종합 판단)")
    opm_ann, opm_year = annual_op_margin(r["코드"], api_key)
    opm_a = result["영업이익률"].mean()

    def below(v, av):
        return pd.notna(v) and pd.notna(av) and v < av

    def above(v, av):
        return pd.notna(v) and pd.notna(av) and v > av

    cheap = sum([below(r["PER"], per_a), below(r["PBR"], pbr_a)])
    pricey = sum([above(r["PER"], per_a), above(r["PBR"], pbr_a)])
    qual = sum([above(r["ROE"], roe_a),
                (opm_ann is not None and above(opm_ann, opm_a))])
    qual_low = sum([below(r["ROE"], roe_a),
                    (opm_ann is not None and below(opm_ann, opm_a))])

    val_state = ("업종 평균보다 저평가" if cheap >= 2 else
                 "업종 평균보다 고평가" if pricey >= 2 else "업종 평균 수준")
    q_state = ("업종 대비 수익성 우수" if qual >= 2 else
               "업종 대비 수익성 부진" if qual_low >= 2 else "업종 평균 수준의 수익성")

    if val_state == "업종 평균보다 저평가" and qual >= 1:
        verdict = "✅ 실적 대비 저평가 매력이 있어 보입니다."
    elif val_state == "업종 평균보다 저평가" and qual_low >= 1:
        verdict = "⚠️ 주가는 싸지만 수익성이 약해 '싼 데는 이유'가 있을 수 있어요."
    elif val_state == "업종 평균보다 고평가" and qual >= 1:
        verdict = "➖ 주가는 다소 비싸나 뛰어난 수익성이 프리미엄을 일부 정당화합니다."
    elif val_state == "업종 평균보다 고평가" and qual_low >= 1:
        verdict = "⚠️ 수익성 대비 주가가 비싼 편이라 고평가 우려가 있습니다."
    else:
        verdict = "➖ 주가·실적 모두 대체로 업종 평균 수준입니다."

    opm_txt = f"{opm_ann:.1f}% ({opm_year}년 연간)" if opm_ann is not None else "자료 없음"

    def jl(v, av):  # 낮을수록 유리
        if pd.isna(v) or pd.isna(av) or av == 0:
            return "-"
        return "저렴" if v < av * 0.98 else ("비쌈" if v > av * 1.02 else "평균 수준")

    def jh(v, av):  # 높을수록 유리
        if pd.isna(v) or pd.isna(av) or av == 0:
            return "-"
        return "우수" if v > av * 1.02 else ("부진" if v < av * 0.98 else "평균 수준")

    def fnum(v, suf=""):
        return f"{v:,.1f}{suf}" if pd.notna(v) else "-"

    tbl = pd.DataFrame(
        [
            ["PER (배)", fnum(r["PER"]), fnum(per_a), jl(r["PER"], per_a)],
            ["PBR (배)", fnum(r["PBR"]), fnum(pbr_a), jl(r["PBR"], pbr_a)],
            ["ROE (%)", fnum(r["ROE"]), fnum(roe_a), jh(r["ROE"], roe_a)],
            ["영업이익률 (연간, %)", fnum(opm_ann), fnum(opm_a), jh(opm_ann, opm_a)],
        ],
        columns=["지표", "기업", "업종평균", "업종 대비"],
    ).set_index("지표")
    st.table(tbl)
    st.success(f"**종합 판단:** {verdict}")
    st.caption("영업이익률은 분기 변동을 줄이기 위해 '연간(사업보고서)' 기준으로 반영했고, "
               "나머지 지표·주가는 선택한 재무 기준과 실시간 시세 기준입니다. "
               "뉴스·리포트는 오른쪽 '📰 뉴스·이슈' 탭에 있어요. 투자 판단 참고용이며 매매 권유가 아닙니다.")


def tab_news(result, focus, sector, allow_pick=True):
    names = result.index.tolist()
    name = focus if focus in names else names[0]
    if allow_pick and len(names) > 1:
        idx = names.index(name)
        name = st.selectbox("종목 선택", names, index=idx, key="news_pick")
    else:
        st.caption(f"종목: **{name}**")
    st.markdown(f"#### 📰 '{name}' 기업 뉴스·이슈")
    render_news(name)
    st.markdown(f"#### 📑 '{name}' 증권사 리포트·리서치")
    render_reports(name, result.loc[name, "코드"])
    if sector:
        st.markdown(f"#### 🏭 '{sector}' 업종 뉴스·이슈")
        render_news(f"{sector} 업종")


def tab_fundamental(result, sub, label):
    st.subheader(f"{label} · {len(result)}종목")
    st.markdown("#### ① 종합 점수표")
    st.dataframe(
        safe_gradient(result[SCORE_COLS].style.format("{:,.1f}"), ["매력도"]),
        use_container_width=True)
    c1, c2 = st.columns([1, 2])
    top = result.index[0]
    c1.metric("🏆 가장 매력적인 종목", top, f"매력도 {result.loc[top, '매력도']:.1f}")
    c2.markdown("**비교군 (다음 4종목)**\n\n" + "  ·  ".join(result.index[1:5]))
    st.markdown("#### ② 세부 지표표")
    unit_names = {"현재가": "현재가(원)", "시총(억)": "시총(억원)",
                  "부채비율": "부채비율(%)", "유동비율": "유동비율(%)", "자기자본비율": "자기자본비율(%)",
                  "ROE": "ROE(%)", "ROA": "ROA(%)", "영업이익률": "영업이익률(%)",
                  "순이익률": "순이익률(%)", "PER": "PER(배)", "PBR": "PBR(배)", "PSR": "PSR(배)"}
    disp = result[DETAIL_COLS].rename(columns=unit_names)
    fmt = {unit_names[c]: "{:,.1f}" for c in DETAIL_COLS}
    fmt["현재가(원)"] = "{:,.0f}"
    fmt["시총(억원)"] = "{:,.0f}"
    st.dataframe(disp.style.format(fmt), use_container_width=True)
    with st.expander("🔍 점수 산정 근거 — 업종 내 표준화(z점수) 평가"):
        st.caption("각 지표를 업종 내에서 표준화(z점수, 값의 크기 차이까지 반영·이상치 ±3σ 보정)해 "
                   "0~100으로 환산하고, 안정성·수익성·밸류에이션 3축으로 종합합니다.")
        order = ["안정성"] + AX_STAB + ["수익성"] + AX_PROF + ["밸류에이션"] + AX_VAL
        st.dataframe(sub[order].style.format("{:,.1f}"), use_container_width=True)
        pick2 = st.selectbox("종목별로 근거 자세히 보기", result.index.tolist(), key="breakdown")
        rank2 = result.index.get_loc(pick2) + 1
        render_basis(pick2, result.loc[pick2], sub.loc[pick2], rank2, len(result))


def tab_technical(result, focus, allow_pick=True):
    names = result.index.tolist()
    st.subheader("주가 · 봉차트 · 지표")
    if allow_pick and len(names) > 1:
        idx = names.index(focus) if focus in names else 0
        pick = st.selectbox("종목 선택", names, index=idx, key="tech")
    else:
        pick = focus if focus in names else names[0]
        st.caption(f"종목: **{pick}**")
    price = get_price(result.loc[pick, "코드"])
    if price is None:
        st.warning("주가 데이터를 받지 못했습니다. 잠시 후 다시 시도해 주세요.")
        return

    with guard("장기 주가 비교"):
        st.markdown("#### 📅 장기 주가 비교 (현재 · 3년 전 · 5년 전)")
        cur = price["Close"].iloc[-1]

        def chg(years):
            tgt = price.index[-1] - pd.DateOffset(years=years)
            s = price[price.index <= tgt]
            return None if len(s) == 0 else (cur / s["Close"].iloc[-1] - 1) * 100

        c0, c3, c5 = st.columns(3)
        c0.metric("현재가", f"{cur:,.0f} 원")
        v3, v5 = chg(3), chg(5)
        c3.metric("3년 전 대비", f"{v3:+.1f}%" if v3 is not None else "-")
        c5.metric("5년 전 대비", f"{v5:+.1f}%" if v5 is not None else "-")
        st.table(price_compare_table(price))

    with guard("투자자 매매동향"):
        render_investor(result.loc[pick, "코드"], pick)

    with guard("봉차트·거래량"):
        st.markdown("#### 📈 봉차트 · 거래량")
        tf = st.radio("봉 주기", list(TF_RULE.keys()), horizontal=True, key="tf")
        o = make_ohlc(price, tf)

        fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                            vertical_spacing=0.04, row_heights=[0.72, 0.28])
        fig.add_trace(go.Candlestick(
            x=o.index, open=o["Open"], high=o["High"], low=o["Low"], close=o["Close"],
            name="주가", increasing_line_color="#e03131", decreasing_line_color="#1c7ed6"),
            row=1, col=1)
        fig.add_trace(go.Scatter(x=o.index, y=o["MA5"], name="MA5", line=dict(width=1, color="#f59f00"),
                                 hovertemplate="MA5 %{y:,.0f}원<extra></extra>"), row=1, col=1)
        fig.add_trace(go.Scatter(x=o.index, y=o["MA20"], name="MA20", line=dict(width=1, color="#7048e8"),
                                 hovertemplate="MA20 %{y:,.0f}원<extra></extra>"), row=1, col=1)
        vol_colors = ["#e03131" if c >= op else "#1c7ed6" for c, op in zip(o["Close"], o["Open"])]
        fig.add_trace(go.Bar(
            x=o.index, y=o.get("Volume"), name="거래량", marker_color=vol_colors,
            hovertemplate="%{x|%Y-%m-%d}<br>거래량 %{y:,.0f}주<extra></extra>"), row=2, col=1)

        fig.update_layout(height=620, xaxis_rangeslider_visible=False, dragmode="pan",
                          margin=dict(l=0, r=0, t=10, b=0), legend=dict(orientation="h"),
                          showlegend=True)
        price_max = float(pd.to_numeric(o["High"], errors="coerce").max())
        vol_max = (float(pd.to_numeric(o["Volume"], errors="coerce").max())
                   if "Volume" in o.columns else 1.0)
        fig.update_yaxes(tickformat=",", ticksuffix="원", hoverformat=",.0f", automargin=True,
                         range=[0, price_max * 1.05], fixedrange=True, row=1, col=1)
        fig.update_yaxes(tickformat=",", ticksuffix="주", automargin=True,
                         range=[0, (vol_max or 1.0) * 1.1], fixedrange=True, row=2, col=1)
        st.plotly_chart(
            fig,
            use_container_width=True,
            config={
                "scrollZoom": True,
                "displayModeBar": True,
                "displaylogo": False,
                "modeBarButtonsToRemove": ["lasso2d", "select2d"],
            },
        )
        st.caption("상단=주가(봉)·이동평균, 하단=거래량. 세로축은 0부터 고정. "
                   "🖱️ 휠=시간축 확대/축소 · 드래그=좌우 이동 · 더블클릭=원래대로. "
                   "봉·거래량 막대에 마우스를 올리면 그 날짜의 값이 표시됩니다.")

        last = o.iloc[-1]
        if pd.notna(last.get("RSI")):
            state = "과매수" if last["RSI"] > 70 else ("과매도" if last["RSI"] < 30 else "중립")
            st.metric(f"RSI(14) · {tf}", f"{last['RSI']:.0f}", state)
        st.caption(f"RSI ({tf} 기준 · 70 위 과매수 · 30 아래 과매도)")
        st.line_chart(o[["RSI"]])


def render_detail(result, sub, label, focus=None, show_summary=False, sector=None, allow_pick=True):
    if show_summary:
        t0, t1, t2, t3 = st.tabs(["📋 분석 요약", "📑 1차 · 기본적 분석",
                                  "📈 2차 · 기술적 분석", "📰 뉴스·이슈"])
        with t0:
            with guard("분석 요약"):
                tab_summary(result, focus or result.index[0], sector=sector, allow_pick=allow_pick)
        with t1:
            with guard("1차 기본적 분석"):
                tab_fundamental(result, sub, label)
        with t2:
            with guard("2차 기술적 분석"):
                tab_technical(result, focus, allow_pick=allow_pick)
        with t3:
            with guard("뉴스·이슈"):
                tab_news(result, focus or result.index[0], sector, allow_pick=allow_pick)
    else:
        t1, t2, t3 = st.tabs(["📑 1차 · 기본적 분석", "📈 2차 · 기술적 분석", "📰 뉴스·이슈"])
        with t1:
            with guard("1차 기본적 분석"):
                tab_fundamental(result, sub, label)
        with t2:
            with guard("2차 기술적 분석"):
                tab_technical(result, focus, allow_pick=allow_pick)
        with t3:
            with guard("뉴스·이슈"):
                tab_news(result, focus or result.index[0], sector, allow_pick=allow_pick)


# ---------- 화면 ----------
st.set_page_config(page_title="KOSPI 종목 분석", layout="wide")
st.title("📊 KOSPI 종목 분석")

api_key = read_api_key()
if not api_key:
    st.info("먼저 OpenDART API 키를 입력하세요. (opendart.fss.or.kr 에서 무료 발급)")
    k = st.text_input("OpenDART API 키", type="password")
    if st.button("저장") and k:
        save_api_key(k)
        st.rerun()
    st.stop()

st.sidebar.header("⚙️ 설정")
mode = st.sidebar.radio("분석 모드",
                        ["단일 업종 상세", "전체 업종 요약", "종목 검색", "주요 시장지표"])

if mode == "주요 시장지표":
    with guard("주요 시장지표"):
        render_market()
    st.stop()

try:
    with st.spinner("KOSPI 상장목록·업종 정보를 불러오는 중... (처음엔 수십 초)"):
        listing = get_kospi_listing()
except Exception as e:
    st.warning(f"상장목록을 받지 못했습니다({e}). 기본 종목군으로 진행합니다.")
    listing = None

marcap_map = dict(zip(listing["코드"], listing["시가총액"])) if listing is not None else {}
price_map = dict(zip(listing["코드"], listing["현재가"])) if listing is not None else {}
valid_sectors = []
if listing is not None:
    valid_sectors = sorted(s for s in listing["업종"].dropna().unique() if s != "(기타)")

# 업종 선택 — 분석 모드 바로 아래 (단일 업종 상세 모드에서만 표시)
sector = None
if mode == "단일 업종 상세" and valid_sectors:
    default_idx = valid_sectors.index("전기전자") if "전기전자" in valid_sectors else 0
    sector = st.sidebar.selectbox("업종 선택", valid_sectors, index=default_idx)

periods = period_options()
plabels = list(periods.keys())
psel = st.sidebar.selectbox("재무 기준 (분기)", plabels, index=0)  # 항상 가장 최근
year, reprt_code = periods[psel]
st.sidebar.caption("항상 가장 최근 분기가 기본. 최신 분기는 공시된 종목만 반영돼요. 주가는 실시간.")
st.sidebar.markdown("**매력도 가중치**")
w_s = st.sidebar.slider("안정성", 0, 100, 40)
w_p = st.sidebar.slider("수익성", 0, 100, 30)
w_v = st.sidebar.slider("밸류에이션", 0, 100, 30)
max_n = st.sidebar.slider("분석 종목 수 (업종 내 시총 상위)", 5, 60, 20)


def run_group(stocks, label, focus=None, show_summary=False, sector=None, allow_pick=True):
    prog = st.progress(0.0, text="재무 데이터 수집 중...")
    metrics = build_metrics(stocks, int(year), reprt_code, api_key, marcap_map, price_map, prog)
    prog.empty()
    if metrics.empty:
        st.error("데이터를 가져오지 못했습니다. 재무 기준(분기)을 바꾸거나 API 키를 확인하세요.")
        st.stop()
    result, sub = score_and_rank(metrics, w_s, w_p, w_v)
    render_detail(result, sub, label, focus=focus, show_summary=show_summary,
                  sector=sector, allow_pick=allow_pick)


if not valid_sectors:
    st.info("업종 정보를 받지 못해 기본 종목군(전기전자)으로 진행합니다.")
    with guard("기본 종목군 분석"):
        run_group(FALLBACK_STOCKS, "전기전자(기본)")
    st.stop()

if mode == "단일 업종 상세":
    sub_list = listing[listing["업종"] == sector].sort_values("시가총액", ascending=False).head(max_n)
    with guard("단일 업종 분석"):
        run_group(dict(zip(sub_list["코드"], sub_list["이름"])), f"'{sector}' 업종 매력도 순위",
                  show_summary=True, sector=sector)

elif mode == "종목 검색":
    names_all = listing.sort_values("시가총액", ascending=False)["이름"].tolist()
    q = st.selectbox("🔎 종목 검색 (이름 입력 시 자동완성)", names_all)
    with guard("종목 검색 분석"):
        rowq = listing[listing["이름"] == q].iloc[0]
        qcode, qsector = rowq["코드"], rowq["업종"]
        st.caption(f"선택: **{q}** ({qcode}) · 업종: {qsector}")
        grp = listing[listing["업종"] == qsector].sort_values("시가총액", ascending=False).head(max_n)
        stocks = dict(zip(grp["코드"], grp["이름"]))
        stocks[qcode] = q
        run_group(stocks, f"'{qsector}' 업종 내 분석", focus=q, show_summary=True,
                  sector=qsector, allow_pick=False)

else:  # 전체 업종 요약
    k = st.sidebar.slider("업종별 분석 종목 수", 3, 15, 5)
    st.info("⏳ KOSPI 전체 업종을 훑습니다. 처음엔 몇 분 걸릴 수 있어요.")
    with guard("전체 업종 요약"):
        uni = (listing[listing["업종"].isin(valid_sectors)]
               .sort_values("시가총액", ascending=False).groupby("업종").head(k))
        prog = st.progress(0.0, text="재무 데이터 수집 중...")
        metrics = build_metrics(dict(zip(uni["코드"], uni["이름"])), int(year), reprt_code,
                                api_key, marcap_map, price_map, prog)
        prog.empty()
        if metrics.empty:
            st.error("데이터를 가져오지 못했습니다. 재무 기준(분기)을 확인하세요.")
            st.stop()
        sector_of = dict(zip(uni["코드"], uni["업종"]))
        metrics["업종"] = metrics["코드"].map(sector_of)
        rows, sector_results = [], {}
        for sec, grp in metrics.groupby("업종"):
            if len(grp) < 2:
                continue
            res, sub_s = score_and_rank(grp.drop(columns=["업종"]), w_s, w_p, w_v)
            sector_results[sec] = (res, sub_s)
            t = res.index[0]
            rows.append({"업종": sec, "대표종목(1위)": t, "매력도": res.loc[t, "매력도"],
                         "안정성": res.loc[t, "안정성"], "수익성": res.loc[t, "수익성"],
                         "밸류에이션": res.loc[t, "밸류에이션"], "비교군": ", ".join(res.index[1:4])})
        summary = pd.DataFrame(rows).sort_values("매력도", ascending=False).set_index("업종")
        st.subheader(f"전체 업종 요약 — 업종별 1위 ({len(summary)}개 업종)")
        st.caption("※ 매력도는 각 업종 내부의 상대 순위입니다.")
        st.dataframe(safe_gradient(summary.style.format({"매력도": "{:,.1f}", "안정성": "{:,.1f}",
                     "수익성": "{:,.1f}", "밸류에이션": "{:,.1f}"}), ["매력도"]),
                     use_container_width=True)
        st.markdown("---")
        sec_pick = st.selectbox("업종 상세 보기", list(sector_results.keys()))
        res, sub = sector_results[sec_pick]
        render_detail(res, sub, f"'{sec_pick}' 업종 상세", show_summary=True, sector=sec_pick)

st.caption("※ 투자 판단과 책임은 본인에게 있습니다. 본 도구는 참고용입니다.")

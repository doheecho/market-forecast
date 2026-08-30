"""analyze.py(주간 AI 전망)와 prices.py(일간 시세 갱신)가 공유하는 수집 로직."""
from __future__ import annotations

import csv
import os
import re
from datetime import datetime

import pandas as pd
import yfinance as yf

TICKERS = {
    "copper": "HG=F", "aluminum": "ALI=F", "wti": "CL=F",
    "gold": "GC=F", "silver": "SI=F", "platinum": "PL=F",
    "steel": "HRC=F", "ironore": "TIO=F",
    "dxy": "DX-Y.NYB", "us10y": "^TNX", "usdcny": "CNY=X", "usdkrw": "KRW=X",
}
# 야후에서 받는 원자재. 야후에 없는 품목(니켈·아연·텅스텐)은 manual/<key>.csv 로 주입.
YF_COMMODITIES = ["wti", "copper", "aluminum", "gold", "silver", "platinum", "steel", "ironore"]
COMMODITIES = list(YF_COMMODITIES)  # 파이프라인이 참조하는 전체 목록 (확장 가능)

MANUAL_DIR = "manual"  # manual/nickel.csv, manual/zinc.csv, manual/tungsten.csv …

# name, unit, 가격 배수(야후 원값 → 표기 단위)
META = {
    "wti": ("WTI 원유 (CME)", "USD/bbl", 1.0),
    "copper": ("전기동 (LME)", "USD/ton", 2204.62),   # HG=F: USD/lb → USD/ton
    "aluminum": ("알루미늄 (LME)", "USD/ton", 1.0),
    "gold": ("금 (LBMA)", "USD/oz.t", 1.0),
    "silver": ("은 (LBMA)", "US￠/oz.t", 100.0),        # SI=F: USD/oz → US¢/oz
    "platinum": ("백금 (CME)", "USD/oz.t", 1.0),
    "steel": ("열연강판 (CME HRC)", "USD/s.ton", 1.0),  # 냉연 SPCC 아님 — 방향성 참고
    "ironore": ("철광석 62%Fe (CFR China)", "USD/dmt", 1.0),
    # 야후에 자유 시세가 없음 — manual/<key>.csv (date,price) 로 주입하면 대시보드에 자동 표시.
    # CSV 가격은 표기 단위 그대로 (배수 적용 안 함).
    "nickel": ("니켈 (LME)", "USD/ton", 1.0),
    "zinc": ("아연 (LME)", "USD/ton", 1.0),
    "tungsten": ("텅스텐 APT", "USD/mtu", 1.0),
}

_DATE_HINTS = ("date", "일자", "기준", "dt", "ymd", "날짜", "거래일", "월")
_PRICE_HINTS = ("price", "close", "종가", "가격", "usd", "값", "시세", "당월", "평균")


def _read_price_csv(path: str) -> list[dict]:
    """date,price 형태의 CSV → [{date:'YYYY-MM-DD', price:float}]. 인코딩/열이름 자동 판별."""
    text = None
    for enc in ("utf-8-sig", "cp949", "euc-kr", "latin1"):
        try:
            with open(path, encoding=enc, newline="") as f:
                text = f.read()
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        print(f"[경고] {path} 인코딩 인식 실패 — 건너뜀")
        return []
    lines = text.splitlines()
    if not lines:
        return []
    head = lines[0]
    delim = "\t" if "\t" in head else (";" if head.count(";") > head.count(",") else ",")
    rows = list(csv.reader(lines, delimiter=delim))
    header = rows[0]

    def pick(hints):
        for i, h in enumerate(header):
            if any(x in h.strip().lower() for x in hints):
                return i
        return -1

    di, pi = pick(_DATE_HINTS), pick(_PRICE_HINTS)
    if di < 0 or pi < 0:
        print(f"[경고] {path}: 날짜/가격 열을 못 찾음 (헤더 {header}) — 건너뜀")
        return []
    seen: dict[str, float] = {}
    for r in rows[1:]:
        if len(r) <= max(di, pi):
            continue
        md = re.match(r"(\d{4})[-./]?(\d{1,2})[-./]?(\d{1,2})?", r[di].strip().strip('"'))
        if not md:
            continue
        y, mo, d = md.group(1), md.group(2), md.group(3) or "01"
        try:
            v = float(re.sub(r"[^\d.\-]", "", r[pi].strip()))
        except ValueError:
            continue
        if v > 0:
            seen[f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"] = round(v, 2)
    return [{"date": k, "price": v} for k, v in sorted(seen.items())]


def load_manual_history() -> dict[str, list[dict]]:
    """manual/<key>.csv 를 모두 읽어 {key: [{date,price}]} 반환. 폴더 없으면 {}."""
    out: dict[str, list[dict]] = {}
    if not os.path.isdir(MANUAL_DIR):
        return out
    for fn in sorted(os.listdir(MANUAL_DIR)):
        if not fn.lower().endswith(".csv"):
            continue
        key = os.path.splitext(fn)[0].strip().lower()
        if key not in META:
            print(f"[경고] manual/{fn}: META 에 없는 키 '{key}' — 무시")
            continue
        rows = _read_price_csv(os.path.join(MANUAL_DIR, fn))
        if len(rows) >= 20:
            out[key] = rows
            print(f"[진행] 수동 시세 로드: {key} ({len(rows)}행, {rows[0]['date']}~{rows[-1]['date']})")
        elif rows:  # 헤더만 있는 빈 템플릿은 조용히 무시, 데이터가 부족할 때만 경고
            print(f"[경고] manual/{fn}: 유효 {len(rows)}행(<20) — 건너뜀")
    return out


def fetch_raw() -> dict[str, pd.Series]:
    """야후 티커 6년 일봉 종가를 yf.download 한 번으로 병렬 수집."""
    print(f"[진행] 야후 파이낸스 시계열 수집 중… ({len(TICKERS)}개 티커 일괄)")
    dl = yf.download(
        list(TICKERS.values()), period="6y", interval="1d",
        auto_adjust=True, progress=False, threads=True, group_by="column",
    )
    try:
        close = dl["Close"]
    except Exception:  # noqa: BLE001
        close = dl
    raw: dict[str, pd.Series] = {}
    for name, tk in TICKERS.items():
        try:
            s = close[tk].dropna()
            if getattr(s.index, "tz", None) is not None:
                s.index = s.index.tz_localize(None)
            raw[name] = s
        except Exception as e:  # noqa: BLE001
            print(f"[경고] {name}({tk}) 수집 실패: {e}")
            raw[name] = pd.Series(dtype=float)
    return raw


def _despike_tail(rows: list[dict]) -> list[dict]:
    """꼬리에 튄 값(야후 마지막 봉 오류): 직전 8개 중앙값 대비 ±12% 초과면 잘라냄."""
    end = len(rows)
    while end > 9:
        ref = sorted(r["price"] for r in rows[end - 9:end - 1])
        med = ref[len(ref) // 2]
        a = rows[end - 1]["price"]
        if med and abs(a / med - 1) > 0.12:
            end -= 1
        else:
            break
    return rows[:end]


def build_history(raw: dict[str, pd.Series]) -> tuple[dict[str, list[dict]], dict[str, float]]:
    """원자재 6종 → 공통 타임라인 정렬 + 배수 적용 + 꼬리 despike. (history, spot) 반환."""
    master = pd.Series(dtype=float)
    for k in COMMODITIES:
        if len(raw.get(k, [])) > len(master):
            master = raw[k]
    timeline = master.index

    history: dict[str, list[dict]] = {}
    spot: dict[str, float] = {}
    for k in COMMODITIES:
        mult = META[k][2]
        s = raw.get(k, pd.Series(dtype=float)).reindex(timeline).ffill().bfill()
        rows = [
            {"date": ts.strftime("%Y-%m-%d"), "price": round(float(v) * mult, 2)}
            for ts, v in s.items()
            if pd.notna(v)
        ]
        rows = _despike_tail(rows)
        history[k] = rows
        if rows:
            spot[k] = rows[-1]["price"]
    return history, spot


def latest_macro(raw: dict[str, pd.Series]) -> dict[str, float]:
    def last(name: str, default: float) -> float:
        s = raw.get(name)
        return round(float(s.iloc[-1]), 4) if s is not None and not s.empty else default

    return {
        "dxy": last("dxy", 104.2),
        "us10y": last("us10y", 4.15),
        "usdcny": last("usdcny", 7.23),
        "usdkrw": last("usdkrw", 1380.0),
    }


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")

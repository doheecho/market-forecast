"""analyze.py(주간 AI 전망)와 prices.py(일간 시세 갱신)가 공유하는 수집 로직."""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import yfinance as yf

TICKERS = {
    "copper": "HG=F", "aluminum": "ALI=F", "wti": "CL=F",
    "gold": "GC=F", "silver": "SI=F", "platinum": "PL=F",
    "dxy": "DX-Y.NYB", "us10y": "^TNX", "usdcny": "CNY=X", "usdkrw": "KRW=X",
}
COMMODITIES = ["wti", "copper", "aluminum", "gold", "silver", "platinum"]

# name, unit, 가격 배수(야후 원값 → 표기 단위)
META = {
    "wti": ("WTI 원유 (CME)", "USD/bbl", 1.0),
    "copper": ("전기동 (LME)", "USD/ton", 2204.62),   # HG=F: USD/lb → USD/ton
    "aluminum": ("알루미늄 (LME)", "USD/ton", 1.0),
    "gold": ("금 (LBMA)", "USD/oz.t", 1.0),
    "silver": ("은 (LBMA)", "US￠/oz.t", 100.0),        # SI=F: USD/oz → US¢/oz
    "platinum": ("백금 (CME)", "USD/oz.t", 1.0),
}


def fetch_raw() -> dict[str, pd.Series]:
    """10개 티커 6년 일봉 종가를 yf.download 한 번으로 병렬 수집."""
    print("[진행] 야후 파이낸스 시계열 수집 중… (10개 티커 일괄)")
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

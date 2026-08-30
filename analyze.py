"""6대 핵심 원자재 AI 가격 전망 생성기.

1. 야후 파이낸스에서 원자재 6종 + 매크로 4종의 시계열을 수집(최근 3년 일봉 + 그 이전 주봉)
2. 요약본을 Gemini 에 전달해 6개월 월별 전망(base/bull/bear) · 시나리오 · 요인지표 ·
   과거 유사국면을 JSON 으로 생성
3. raw_materials_forecast.json 으로 저장 (실패 시 기존 파일 유지)
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    sys.exit("[에러] GEMINI_API_KEY 환경변수가 없습니다.")

# 모델: 환경변수로 재정의 가능. 앞에서부터 순서대로 시도.
MODELS = [m.strip() for m in os.environ.get("GEMINI_MODEL", "").split(",") if m.strip()] or [
    "gemini-flash-latest",
    "gemini-2.5-flash",
    "gemini-flash-lite-latest",
]

try:
    from google import genai
    from google.genai import types

    _client = genai.Client(api_key=API_KEY)
    _NEW_SDK = True
except ImportError:
    import google.generativeai as _legacy

    _legacy.configure(api_key=API_KEY)
    _NEW_SDK = False

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


def fetch_series(ticker: str) -> pd.Series:
    """최근 3년은 일봉, 그 이전 7년은 주봉으로 이어붙인 종가 시계열."""
    today = datetime.now()
    d3 = today - timedelta(days=3 * 365)
    d10 = today - timedelta(days=10 * 365)
    try:
        tk = yf.Ticker(ticker)
        daily = tk.history(start=d3, end=today, interval="1d", auto_adjust=True)["Close"]
        weekly = tk.history(start=d10, end=d3, interval="1wk", auto_adjust=True)["Close"]
        s = pd.concat([weekly, daily]).sort_index()
        s = s[~s.index.duplicated(keep="last")].dropna()
        s.index = s.index.tz_localize(None)
        return s
    except Exception as e:  # noqa: BLE001
        print(f"[경고] {ticker} 수집 실패: {e}")
        return pd.Series(dtype=float)


print("[진행] 야후 파이낸스 시계열 수집 중…")
raw = {name: fetch_series(tk) for name, tk in TICKERS.items()}

# 원자재 6종을 하나의 타임라인으로 정렬 (가장 긴 시계열 기준, 결측은 직전값으로 보간)
master = pd.Series(dtype=float)
for k in COMMODITIES:
    if len(raw[k]) > len(master):
        master = raw[k]
timeline = master.index

history: dict[str, list[dict]] = {}
for k in COMMODITIES:
    mult = META[k][2]
    s = raw[k].reindex(timeline).ffill().bfill()
    history[k] = [
        {"date": ts.strftime("%Y-%m-%d"), "price": round(float(v) * mult, 2)}
        for ts, v in s.items()
        if pd.notna(v)
    ]

if not any(history.values()):
    sys.exit("[에러] 원자재 시계열을 하나도 수집하지 못했습니다.")

# AI 프롬프트용 다운샘플 (~120 포인트)
history_brief = {}
for k, rows in history.items():
    step = max(1, len(rows) // 120)
    history_brief[k] = rows[::step]


def last_val(name: str, default: float) -> float:
    s = raw.get(name)
    return round(float(s.iloc[-1]), 4) if s is not None and not s.empty else default


update_date = datetime.now().strftime("%Y-%m-%d")
macro = {
    "dxy": last_val("dxy", 104.2),
    "us10y": last_val("us10y", 4.15),
    "usdcny": last_val("usdcny", 7.23),
    "usdkrw": last_val("usdkrw", 1380.0),
}
market_input = {"update_date": update_date, "macro": macro, "history_summary": history_brief}

SCHEMA_ONE = """{
  "name": "WTI 원유 (CME)", "unit": "USD/bbl",
  "current_price": 0.0, "forecast_6m_target": 0.0, "forecast_change_rate": "+0.0%", "volatility_score": 0,
  "planning_advisor": "구매/헤지 담당자를 위한 한 문장 전략 코멘트",
  "monthly_forecast_base": [ {"month": "2026-09", "price": 0.0, "rationale": "해당 월 가격 산정 근거 한 문장"} ],
  "monthly_forecast_bull": [ {"month": "2026-09", "price": 0.0} ],
  "monthly_forecast_bear": [ {"month": "2026-09", "price": 0.0} ],
  "rationale_base": "기본 시나리오 요약", "rationale_bull": "낙관 요약", "rationale_bear": "비관 요약",
  "metrics": [ {"label": "위안화 환율", "val": "6.7222 (USD/CNY)", "date": "2026.08.27", "cat": "수요", "status": "보통", "badge": "secondary"} ],
  "analogs": [ {"period": "'20.11~'21.10", "similarity": "92%", "acc": "92%", "forecast": "+14.9%", "actual": "+3.8%",
    "title": "역사적 사건 정성 제목", "summary": "국면 요약",
    "miniHist": [12개 월 과거 가격], "miniForecast": [이후 6개 월 실제 가격]} ]
}"""

prompt = f"""당신은 글로벌 원자재/거시경제 퀀트 애널리스트입니다.
아래 시장 입력 데이터를 바탕으로 6대 원자재(wti, copper, aluminum, gold, silver, platinum)의
6개월 가격 전망 데이터셋을 순수 JSON 으로만 작성하세요. 마크다운/설명 금지.

규칙:
- monthly_forecast_* 는 {update_date} 기준 이후 6개 월 (예: 2026-09 ~ 2027-02).
- badge 는 danger/warning/success/secondary 중 하나. cat 은 공급/수요/투자/매크로 중 하나.
- metrics 3~5개, analogs 1~2개. miniHist 12개, miniForecast 6개 숫자.
- 단위: wti USD/bbl, copper·aluminum USD/ton, gold·platinum USD/oz.t, silver US￠/oz.t.

[시장 입력 데이터]
{json.dumps(market_input, ensure_ascii=False)}

응답 스키마 (commodities 의 각 값은 아래 형태, name/unit 은 원자재에 맞게):
{{ "update_date": "{update_date}", "commodities": {{ "wti": {SCHEMA_ONE}, "copper": {{...}}, "aluminum": {{...}}, "gold": {{...}}, "silver": {{...}}, "platinum": {{...}} }} }}
"""


def call_gemini(text: str) -> str:
    last = None
    for model in MODELS:
        for attempt in range(1, 4):
            try:
                print(f"[진행] {model} 호출 (시도 {attempt})…")
                if _NEW_SDK:
                    r = _client.models.generate_content(
                        model=model, contents=text,
                        config=types.GenerateContentConfig(response_mime_type="application/json"),
                    )
                    return r.text
                m = _legacy.GenerativeModel(model)
                return m.generate_content(
                    text, generation_config={"response_mime_type": "application/json"}
                ).text
            except Exception as e:  # noqa: BLE001
                last = e
                wait = 2 ** attempt + 3
                print(f"[경고] {model} 실패: {e} → {wait}s 후 재시도")
                time.sleep(wait)
    raise RuntimeError(f"Gemini 전체 실패: {last}")


def validate(commodities: dict) -> None:
    need = {"name", "unit", "current_price", "forecast_6m_target",
            "monthly_forecast_base", "rationale_base"}
    missing = [k for k in COMMODITIES if k not in commodities]
    if missing:
        raise ValueError(f"누락된 원자재: {missing}")
    for k in COMMODITIES:
        gaps = need - set(commodities[k])
        if gaps:
            raise ValueError(f"{k} 필드 누락: {sorted(gaps)}")
        if not commodities[k]["monthly_forecast_base"]:
            raise ValueError(f"{k} monthly_forecast_base 비어 있음")


print("[진행] Gemini 전망 생성…")
try:
    parsed = json.loads(call_gemini(prompt))
    commodities = parsed["commodities"]
    validate(commodities)
except Exception as e:  # noqa: BLE001
    print(f"[에러] 전망 생성/검증 실패: {e}. 기존 raw_materials_forecast.json 유지.")
    sys.exit(1)

output = {
    "update_date": update_date,
    "macro": macro,
    "history_3y": history,   # (호환) 키 이름 유지 — 실제로는 최대 10년치
    "forecast_data": commodities,
}
with open("raw_materials_forecast.json", "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)
print("[성공] raw_materials_forecast.json 저장 완료")

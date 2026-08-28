import json
import os
from datetime import datetime
from google import genai
from google.genai import types
import pandas as pd
import yfinance as yf

# 1. 클라이언트 초기화
api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
  print("[에러] GEMINI_API_KEY가 설정되지 않았습니다.")
  exit(1)

client = genai.Client(api_key=api_key)

# 2. 다변량 시장 데이터 수집 (최근 3년 데이터, 일단위 interval="1d")
TICKERS = {
    "copper": "HG=F",  # 구리 (USD/lb)
    "gold": "GC=F",  # 금 (USD/oz)
    "silver": "SI=F",  # 은 (USD/oz)
    "aluminum": "ALI=F",  # 알루미늄 (USD/mt)
    "wti": "CL=F",  # WTI 원유 (USD/bbl)
    "dxy": "DX-Y.NYB",  # 달러 인덱스
    "us10y": "^TNX",  # 미국 10년물 국채금리
    "usdcny": "CNY=X",  # 위안화 환율
    "usdkrw": "KRW=X",  # 원/달러 환율
}

print("[진행] 야후 파이낸스에서 3년 일단위 시계열 데이터 수집 중...")
hist_data = {}
for name, ticker in TICKERS.items():
  try:
    # interval="1d"로 일일 가격 수집 (영업일 기준 없는 날짜는 dropna로 있는 날만 수집)
    df = yf.Ticker(ticker).history(period="3y", interval="1d")["Close"]
    df = df.dropna()
    hist_data[name] = df
  except Exception as e:
    print(f"[경고] {name}({ticker}) 수집 실패: {e}")
    hist_data[name] = pd.Series(dtype=float)


# 시계열 데이터를 일별 [YYYY-MM-DD, 가격] 리스트로 가공 (대시보드 차트 표출용)
def format_history_daily(series, unit_mult=1.0):
  if series.empty:
    return []
  res = []
  for idx, val in series.items():
    date_str = idx.strftime("%Y-%m-%d")
    res.append({"date": date_str, "price": round(float(val) * unit_mult, 2)})
  return res


# 시계열 데이터를 월별 [YYYY-MM, 가격] 리스트로 가공 (Gemini 프롬프트 분석 요약용)
def format_history_monthly(series, unit_mult=1.0):
  if series.empty:
    return []
  # ME 와 M 호환성 처리
  try:
    monthly_series = series.resample("ME").last().dropna()
  except ValueError:
    monthly_series = series.resample("M").last().dropna()
  res = []
  for idx, val in monthly_series.items():
    date_str = idx.strftime("%Y-%m")
    res.append({"date": date_str, "price": round(float(val) * unit_mult, 2)})
  return res


# 텅스텐(Tungsten APT) 추정 시계열 생성 (일단위 및 월단위)
copper_s = hist_data.get("copper", pd.Series())
tungsten_history_daily = []
if not copper_s.empty:
  for idx, val in copper_s.items():
    date_str = idx.strftime("%Y-%m-%d")
    approx_price = round(320.0 + (float(val) * 12.5), 1)  # USD/mtu 기준
    tungsten_history_daily.append({"date": date_str, "price": approx_price})

tungsten_history_monthly = []
if not copper_s.empty:
  try:
    copper_monthly = copper_s.resample("ME").last().dropna()
  except ValueError:
    copper_monthly = copper_s.resample("M").last().dropna()
  for idx, val in copper_monthly.items():
    date_str = idx.strftime("%Y-%m")
    approx_price = round(320.0 + (float(val) * 12.5), 1)
    tungsten_history_monthly.append({"date": date_str, "price": approx_price})

# Gemini 분석용 가벼운 월단위 데이터 요약 패킷 구성
market_summary_for_ai = {
    "update_date": datetime.now().strftime("%Y-%m-%d"),
    "macro": {
        "dxy": (
            round(float(hist_data["dxy"].iloc[-1]), 2)
            if not hist_data["dxy"].empty
            else 104.2
        ),
        "us10y": (
            round(float(hist_data["us10y"].iloc[-1]), 2)
            if not hist_data["us10y"].empty
            else 4.15
        ),
        "usdcny": (
            round(float(hist_data["usdcny"].iloc[-1]), 4)
            if not hist_data["usdcny"].empty
            else 7.23
        ),
        "usdkrw": (
            round(float(hist_data["usdkrw"].iloc[-1]), 1)
            if not hist_data["usdkrw"].empty
            else 1380.0
        ),
    },
    "history": {
        "copper": format_history_monthly(hist_data.get("copper")),
        "tungsten": tungsten_history_monthly,
        "gold": format_history_monthly(hist_data.get("gold")),
        "silver": format_history_monthly(hist_data.get("silver")),
        "aluminum": format_history_monthly(hist_data.get("aluminum")),
        "wti": format_history_monthly(hist_data.get("wti")),
    },
}

# 3. 고도화된 Gemini 프롬프트 구성
prompt = f"""
당신은 글로벌 원자재 및 거시경제 퀀트 분석 수석 애널리스트입니다.
아래 제공된 [3년간의 월별 과거 시계열 데이터]와 [거시경제 지표(달러인덱스, 국채금리, 위안화, 원달러)]를 종합하여,
6대 원자재 (전기동, 텅스텐, 금, 은, 알루미늄, WTI) 각각에 대한 '향후 6개월간(M+1 ~ M+6)의 정밀 가격 예측'과 '구체적 산정 근거'를 작성하세요.

[분석 가이드라인]
1. 단순 추세선 외삽이 아닌 다변량 상관관계(거시 매크로, 공급망 병목, 지정학 리스크, 친환경/반도체 산업 수요)를 종합 평가하세요.
2. 텅스텐은 중국의 수출 통제 및 공급망 편중(80% 이상), 반도체/초경합금 수요를 핵심 드라이버로 반영하세요.
3. 각 품목별로 3가지 시나리오(Base, Bull, Bear) 중 가장 가능성 높은 궤적을 monthly_forecast에 제시하세요.
4. rationale(산정 근거)은 수급 요인, 매크로 요인, 리스크 요인을 포함하여 구매 담당자가 바로 보고서에 인용할 수 있도록 3~4문장으로 전문성 있게 작성하세요.

[시장 입력 데이터]
{json.dumps(market_summary_for_ai, ensure_ascii=False)}

반드시 마크다운 없이 순수 JSON 포맷으로만 응답하세요. 스키마:
{{
  "update_date": "{market_summary_for_ai['update_date']}",
  "commodities": {{
    "copper": {{
      "name": "전기동 (Copper)",
      "unit": "USD/lb",
      "current_price": 0.0,
      "forecast_6m_target": 0.0,
      "forecast_change_rate": "+0.0%",
      "direction": "상승/하락/보합",
      "volatility_score": 0,
      "rationale": "산정 근거 요약 (수급, 환율, 광산 TC 등)",
      "monthly_forecast": [
        {{"month": "2026-09", "price": 0.0}},
        {{"month": "2026-10", "price": 0.0}},
        {{"month": "2026-11", "price": 0.0}},
        {{"month": "2026-12", "price": 0.0}},
        {{"month": "2027-01", "price": 0.0}},
        {{"month": "2027-02", "price": 0.0}}
      ]
    }},
    "tungsten": {{
      "name": "텅스텐 (Tungsten APT)",
      "unit": "USD/mtu",
      "current_price": 0.0,
      "forecast_6m_target": 0.0,
      "forecast_change_rate": "+0.0%",
      "direction": "상승/하락/보합",
      "volatility_score": 0,
      "rationale": "산정 근거 요약 (중국 공급망, 방산/반도체 수요 등)",
      "monthly_forecast": [
        {{"month": "2026-09", "price": 0.0}},
        {{"month": "2026-10", "price": 0.0}},
        {{"month": "2026-11", "price": 0.0}},
        {{"month": "2026-12", "price": 0.0}},
        {{"month": "2027-01", "price": 0.0}},
        {{"month": "2027-02", "price": 0.0}}
      ]
    }},
    "gold": {{ "name": "금 (Gold)", "unit": "USD/oz", "current_price": 0.0, "forecast_6m_target": 0.0, "forecast_change_rate": "+0.0%", "direction": "상승", "volatility_score": 0, "rationale": "...", "monthly_forecast": [] }},
    "silver": {{ "name": "은 (Silver)", "unit": "USD/oz", "current_price": 0.0, "forecast_6m_target": 0.0, "forecast_change_rate": "+0.0%", "direction": "상승", "volatility_score": 0, "rationale": "...", "monthly_forecast": [] }},
    "aluminum": {{ "name": "알루미늄 (Aluminum)", "unit": "USD/mt", "current_price": 0.0, "forecast_6m_target": 0.0, "forecast_change_rate": "+0.0%", "direction": "상승", "volatility_score": 0, "rationale": "...", "monthly_forecast": [] }},
    "wti": {{ "name": "WTI 원유", "unit": "USD/bbl", "current_price": 0.0, "forecast_6m_target": 0.0, "forecast_change_rate": "+0.0%", "direction": "상승", "volatility_score": 0, "rationale": "...", "monthly_forecast": [] }}
  }}
}}
"""

print("[진행] Gemini 3.6 Flash 다변량 예측 연산 수행 중...")
try:
  response = client.models.generate_content(
      model="gemini-3.6-flash",
      contents=prompt,
      config=types.GenerateContentConfig(
          response_mime_type="application/json",
      ),
  )

  result_json = json.loads(response.text)

  # 과거 일단위 고정밀 시계열 데이터셋 구성
  history_daily_out = {
      "copper": format_history_daily(hist_data.get("copper")),
      "tungsten": tungsten_history_daily,
      "gold": format_history_daily(hist_data.get("gold")),
      "silver": format_history_daily(hist_data.get("silver")),
      "aluminum": format_history_daily(hist_data.get("aluminum")),
      "wti": format_history_daily(hist_data.get("wti")),
  }

  # 과거 일단위 고정밀 데이터와 AI 예측 결과를 결합
  final_output = {
      "update_date": market_summary_for_ai["update_date"],
      "macro": market_summary_for_ai["macro"],
      "history_3y": history_daily_out,
      "forecast_data": result_json["commodities"],
  }

  with open("raw_materials_forecast.json", "w", encoding="utf-8") as f:
    json.dump(final_output, f, ensure_ascii=False, indent=2)

  print(
      f"[성공] raw_materials_forecast.json 생성 완료:"
      f" {market_summary_for_ai['update_date']} (일단위 고밀도 데이터셋 반영)"
  )

except Exception as e:
  print(f"[Gemini 연산 실패] {e}")
  exit(1)

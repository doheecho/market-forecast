import json
import os
import time
from datetime import datetime, timedelta
import pandas as pd
import yfinance as yf

api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
  print("[에러] GEMINI_API_KEY가 설정되지 않았습니다.")
  exit(1)

try:
  from google import genai
  from google.genai import types
  client = genai.Client(api_key=api_key)
  use_new_sdk = True
except ImportError:
  import google.generativeai as google_genai
  google_genai.configure(api_key=api_key)
  use_new_sdk = False

TICKERS = {
    "copper": "HG=F",      "aluminum": "ALI=F",   "wti": "CL=F",
    "gold": "GC=F",        "silver": "SI=F",      "platinum": "PL=F",
    "dxy": "DX-Y.NYB",     "us10y": "^TNX",       "usdcny": "CNY=X",     "usdkrw": "KRW=X"
}

print("[진행] 야후 파이낸스 10개년 입체 수집 중 (최근 3년 일간 '1d' + 과거 3~10년전 주간 '1wk')...")
hist_data = {}
today = datetime.now()
three_y = today - timedelta(days=3*365)
ten_y = today - timedelta(days=10*365)

for name, ticker in TICKERS.items():
  try:
    df_d = yf.Ticker(ticker).history(start=three_y.strftime("%Y-%m-%d"), end=today.strftime("%Y-%m-%d"), interval="1d")["Close"]
    df_w = yf.Ticker(ticker).history(start=ten_y.strftime("%Y-%m-%d"), end=three_y.strftime("%Y-%m-%d"), interval="1wk")["Close"]
    df_m = pd.concat([df_w, df_d]).sort_index().dropna()
    hist_data[name] = df_m
  except Exception as e:
    print(f"[경고] {name} 수집 실패: {e}")
    hist_data[name] = pd.Series(dtype=float)

def generate_6_commodities_10y():
  p_series = hist_data.get("copper", pd.Series())
  if p_series.empty: p_series = hist_data.get("wti", pd.Series())
  res = {k: [] for k in ["wti", "copper", "aluminum", "gold", "silver", "platinum"]}
  for idx, val in p_series.items():
    date_str = idx.strftime("%Y-%m-%d")
    try: cop = float(val)
    except: cop = 4.0
    try: wti = float(hist_data["wti"].loc[idx]) if idx in hist_data["wti"].index else 75.0
    except: wti = 75.0
    try: alu = float(hist_data["aluminum"].loc[idx]) if idx in hist_data["aluminum"].index else 2200.0
    except: alu = 2200.0
    try: gold = float(hist_data["gold"].loc[idx]) if idx in hist_data["gold"].index else 2300.0
    except: gold = 2300.0
    try: sil = float(hist_data["silver"].loc[idx]) if idx in hist_data["silver"].index else 28.0
    except: sil = 28.0
    try: plat = float(hist_data["platinum"].loc[idx]) if idx in hist_data["platinum"].index else 1000.0
    except: plat = 1000.0

    res["wti"].append({"date": date_str, "price": round(wti, 2)})
    res["copper"].append({"date": date_str, "price": round(cop * 2204.62, 1)})
    res["aluminum"].append({"date": date_str, "price": round(alu, 1)})
    res["gold"].append({"date": date_str, "price": round(gold, 2)})
    res["silver"].append({"date": date_str, "price": round(sil, 2)})
    res["platinum"].append({"date": date_str, "price": round(plat, 2)})
  return res

res_daily = generate_6_commodities_10y()
summary_history = {}
for k, v in res_daily.items():
  step = max(1, len(v) // 60)
  summary_history[k] = v[::step]

market_summary_for_ai = {
    "update_date": datetime.now().strftime("%Y-%m-%d"),
    "macro": {
        "dxy": round(float(hist_data["dxy"].iloc[-1]), 2) if not hist_data["dxy"].empty else 104.2,
        "us10y": round(float(hist_data["us10y"].iloc[-1]), 2) if not hist_data["us10y"].empty else 4.15,
        "usdcny": round(float(hist_data["usdcny"].iloc[-1]), 4) if not hist_data["usdcny"].empty else 7.23,
        "usdkrw": round(float(hist_data["usdkrw"].iloc[-1]), 1) if not hist_data["usdkrw"].empty else 1380.0
    },
    "history_summary": summary_history
}

prompt = f"""
당신은 글로벌 원자재 및 거시경제 퀀트 분석 수석 애널리스트입니다.
시장 입력 데이터를 참고하여, 6대 핵심 원자재(wti, copper, aluminum, gold, silver, platinum) 각각의 예측가와 시나리오, 요인지표(metrics), 과거 유사국면(analogs) 분석 데이터셋을 마크다운 없이 순수 JSON 포맷으로 작성하세요.

[요인지표(metrics) 수급 요약 가이드]
- Rationale(수급 요약)에는 공급 요인의 칠레 구리 광산 생산량 감소 폭 확대와 수요 요인의 위안화 환율 하락이 가격 상방을 지지하고 있습니다 처럼 개별 지표의 연계를 Rationale 문장으로 직접 쓰세요.

[과거 유사국면(analogs) 가이드]
- title: "남아공 제련소 차질 및 자동차 촉매 대체 수요 국면", "중국 부양책 랠리 및 LME 공급 병목 국면" 처럼 역사적 사건을 직접 대제목으로 기입.
- period: 연도 약식 포맷 (예: "'20.11~'21.10")
- miniHist(과거 12개 월 가격), miniForecast(과거 유사 시점 이후 실제 6개 월 가격 결과)

[6개월 가격 예측 근거 가이드]
- monthly_forecast_base 내의 "rationale" 속성에는 각 월에 매칭되는 실제 AI 가격 산정 고유의 근거 텍스트 데이터를 작성하세요.

[시장 입력 데이터]
{json.dumps(market_summary_for_ai, ensure_ascii=False)}

스키마:
{{
  "update_date": "{market_summary_for_ai['update_date']}",
  "commodities": {{
    "wti": {{ 
      "name": "WTI 원유 (CME)", "unit": "USD/bbl", "current_price": 0.0, "forecast_6m_target": 0.0, "forecast_change_rate": "+0.0%", "volatility_score": 0, 
      "planning_advisor": "Planning Advisor : [전략 문구]",
      "monthly_forecast_base": [ 
        {{"month": "2026-09", "price": 0.0, "rationale": "9월 실질 수급 및 매크로 지표 변동에 따른 AI 정밀 산정 근거 데이터"}} 
      ],
      "monthly_forecast_bull": [ {{"month": "2026-09", "price": 0.0}} ],
      "monthly_forecast_bear": [ {{"month": "2026-09", "price": 0.0}} ],
      "rationale_base": "기본 요약", "rationale_bull": "낙관 요약", "rationale_bear": "비관 요약",
      "metrics": [ {{"label": "위안화 환율", "val": "6.72223 (USD/CNY)", "date": "2026.08.27", "cat": "수요", "status": "보통", "badge": "secondary"}} ],
      "analogs": [
        {{ "period": "'20.11~'21.10", "similarity": "92%", "acc": "92%", "forecast": "+14.9%", "actual": "+3.8%", "title": "남아공 제련소 차질 및 자동차 촉매 대체 수요 국면", "summary": "공급 측면의 구리 TC 하락이...",
          "miniHist": [51, 52, 55, 58, 61, 63, 65, 68, 72, 75, 80, 85],
          "miniForecast": [88, 90, 92, 94, 96, 98]
        }}
      ]
    }},
    "copper": {{ "name": "전기동 (LME)", "unit": "USD/ton", "current_price": 0.0, "forecast_6m_target": 0.0, "forecast_change_rate": "+0.0%", "volatility_score": 0, "planning_advisor": "Planning Advisor : [전략 문구]", "monthly_forecast_base": [ {{"month": "2026-09", "price": 0.0, "rationale": "근거"}} ], "monthly_forecast_bull": [ {{"month": "2026-09", "price": 0.0}} ], "monthly_forecast_bear": [ {{"month": "2026-09", "price": 0.0}} ], "rationale_base": "기본", "rationale_bull": "낙관", "rationale_bear": "비관", "metrics": [ {{"label": "구리 TC", "val": "-227.5 (USD/mt)", "date": "2026.08.27", "cat": "공급", "status": "강세", "badge": "danger"}} ], "analogs": [ {{ "period": "'20.11~'21.10", "similarity": "92%", "acc": "92%", "forecast": "+14.9%", "actual": "+3.8%", "title": "남아공 제련소 차질 및 자동차 촉매 대체 수요 국면", "summary": "요약", "miniHist": [5000,5200,5400,5600,5800,6000,6200,6400,6600,6800,7000,7200], "miniForecast": [7400,7600,7800,8000,8200,8400] }} ] }},
    "aluminum": {{ "name": "알루미늄 (LME)", "unit": "USD/ton", "current_price": 0.0, "forecast_6m_target": 0.0, "forecast_change_rate": "+0.0%", "volatility_score": 0, "planning_advisor": "Planning Advisor : [전략 문구]", "monthly_forecast_base": [ {{"month": "2026-09", "price": 0.0, "rationale": "근거"}} ], "monthly_forecast_bull": [ {{"month": "2026-09", "price": 0.0}} ], "monthly_forecast_bear": [ {{"month": "2026-09", "price": 0.0}} ], "rationale_base": "기본", "rationale_bull": "낙관", "rationale_bear": "비관", "metrics": [ {{"label": "제련 전력비", "val": "142.5 (EUR/MWh)", "date": "2026.08.27", "cat": "공급", "status": "보통", "badge": "secondary"}} ], "analogs": [ {{ "period": "'16.09~'17.08", "similarity": "86%", "acc": "98%", "forecast": "+8.3%", "actual": "+8.2%", "title": "중국 인프라 투자 국면", "summary": "요약", "miniHist": [1800,1850,1900,1950,2000,2050,2100,2150,2200,2250,2300,2350], "miniForecast": [2400,2450,2500,2550,2600,2650] }} ] }},
    "gold": {{ "name": "금 (LBMA)", "unit": "USD/oz.t", "current_price": 0.0, "forecast_6m_target": 0.0, "forecast_change_rate": "+0.0%", "volatility_score": 0, "planning_advisor": "Planning Advisor : [전략 문구]", "monthly_forecast_base": [ {{"month": "2026-09", "price": 0.0, "rationale": "근거"}} ], "monthly_forecast_bull": [ {{"month": "2026-09", "price": 0.0}} ], "monthly_forecast_bear": [ {{"month": "2026-09", "price": 0.0}} ], "rationale_base": "기본", "rationale_bull": "낙관", "rationale_bear": "비관", "metrics": [ {{"label": "10년물 실질금리", "val": "1.85 (Percent)", "date": "2026.08.27", "cat": "투자", "status": "강세", "badge": "danger"}} ], "analogs": [ {{ "period": "'19.01~'19.12", "similarity": "88%", "acc": "95%", "forecast": "+11.5%", "actual": "+10.2%", "title": "연준 금리 동결 국면", "summary": "요약", "miniHist": [1200,1250,1300,1350,1400,1450,1500,1550,1600,1650,1700,1750], "miniForecast": [1800,1850,1900,1950,2000,2050] }} ] }},
    "silver": {{ "name": "은 (LBMA)", "unit": "US￠/oz.t", "current_price": 0.0, "forecast_6m_target": 0.0, "forecast_change_rate": "+0.0%", "volatility_score": 0, "planning_advisor": "Planning Advisor : [전략 문구]", "monthly_forecast_base": [ {{"month": "2026-09", "price": 0.0, "rationale": "근거"}} ], "monthly_forecast_bull": [ {{"month": "2026-09", "price": 0.0}} ], "monthly_forecast_bear": [ {{"month": "2026-09", "price": 0.0}} ], "rationale_base": "기본", "rationale_bull": "낙관", "rationale_bear": "비관", "metrics": [ {{"label": "태양광용 은 수요", "val": "4250 (TONNES)", "date": "2026.08.27", "cat": "수요", "status": "강세", "badge": "danger"}} ], "analogs": [ {{ "period": "'19.01~'19.12", "similarity": "88%", "acc": "95%", "forecast": "+11.5%", "actual": "+10.2%", "title": "태양광 전도체 수요 랠리 국면", "summary": "요약", "miniHist": [15,15.5,16,16.5,17,17.5,18,18.5,19,19.5,20,20.5], "miniForecast": [21,21.5,22,22.5,23,23.5] }} ] }},
    "platinum": {{ "name": "백금 (CME)", "unit": "USD/oz.t", "current_price": 0.0, "forecast_6m_target": 0.0, "forecast_change_rate": "+0.0%", "volatility_score": 0, "planning_advisor": "Planning Advisor : [전략 문구]", "monthly_forecast_base": [ {{"month": "2026-09", "price": 0.0, "rationale": "근거"}} ], "monthly_forecast_bull": [ {{"month": "2026-09", "price": 0.0}} ], "monthly_forecast_bear": [ {{"month": "2026-09", "price": 0.0}} ], "rationale_base": "기본", "rationale_bull": "낙관", "rationale_bear": "비관", "metrics": [ {{"label": "남아공 광산 원가", "val": "보통 (Level)", "date": "2026.08.27", "cat": "공급", "status": "강세", "badge": "danger"}} ], "analogs": [ {{ "period": "'19.01~'19.12", "similarity": "88%", "acc": "95%", "forecast": "+11.5%", "actual": "+10.2%", "title": "남아공 대정전 및 노조 파업 국면", "summary": "요약", "miniHist": [800,830,850,880,900,920,950,980,1000,1020,1050,1080], "miniForecast": [1100,1120,1150,1180,1200,1220] }} ] }}
  }}
}}
"""

print("[진행] Gemini 다변량 퀀트 오버레이 패턴 매칭 분석 가동...")
MODELS_TO_TRY = ["gemini-3.6-flash", "gemini-3.1-pro-preview"]
response = None
last_exception = None
success = False

for model_name in MODELS_TO_TRY:
  try:
    print(f"[진행] {model_name} 연산 시도 중...")
    if use_new_sdk:
      response = client.models.generate_content(
          model=model_name,
          contents=prompt,
          config=types.GenerateContentConfig(response_mime_type="application/json")
      )
    else:
      model = google_genai.GenerativeModel(model_name)
      response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
    success = True
    break
  except Exception as e:
    last_exception = e
    print(f"[경고] {model_name} 실패: {e}. 다음 모델 폴백...")
    time.sleep(2)

if not success:
  print(f"[최종 에러] Gemini 연산 실패: {last_exception}")
  exit(1)

try:
  result_json = json.loads(response.text)
  final_output = {
      "update_date": market_summary_for_ai["update_date"],
      "macro": market_summary_for_ai["macro"],
      "history_3y": res_daily,
      "forecast_data": result_json["commodities"],
  }
  with open("raw_materials_forecast.json", "w", encoding="utf-8") as f:
    json.dump(final_output, f, ensure_ascii=False, indent=2)
  print(f"[성공] raw_materials_forecast.json 생성 완료!")
except Exception as e:
  print(f"[JSON 파싱 실패] {e}")
  exit(1)

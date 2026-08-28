import json
import os
import time
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

# 2. 다변량 촘촘한 일단위 데이터 수집 (interval="1d")
TICKERS = {
    "copper": "HG=F",      "aluminum": "ALI=F",   "wti": "CL=F",
    "gold": "GC=F",        "silver": "SI=F",      "platinum": "PL=F",
    "dxy": "DX-Y.NYB",     "us10y": "^TNX",       "usdcny": "CNY=X",     "usdkrw": "KRW=X"
}

print("[진행] 야후 파이낸스에서 3개년 촘촘한 일단위(1d) 가격 시계열 데이터 수집 중...")
hist_data = {}
for name, ticker in TICKERS.items():
  try:
    df = yf.Ticker(ticker).history(period="3y", interval="1d")["Close"]
    df = df.dropna()
    hist_data[name] = df
  except Exception as e:
    print(f"[경고] {name}({ticker}) 수집 실패: {e}")
    hist_data[name] = pd.Series(dtype=float)

def generate_6_commodities_daily():
  pivot_series = hist_data.get("copper", pd.Series())
  if pivot_series.empty: pivot_series = hist_data.get("wti", pd.Series())
  res = {k: [] for k in ["wti", "copper", "aluminum", "gold", "silver", "platinum"]}
  for idx, val in pivot_series.items():
    date_str = idx.strftime("%Y-%m-%d")
    try: cop = float(val)
    except: cop = 4.0
    try: wti_val = float(hist_data["wti"].loc[idx]) if idx in hist_data["wti"].index else 75.0
    except: wti_val = 75.0
    try: alu_val = float(hist_data["aluminum"].loc[idx]) if idx in hist_data["aluminum"].index else 2200.0
    except: alu_val = 2200.0
    try: gold_val = float(hist_data["gold"].loc[idx]) if idx in hist_data["gold"].index else 2300.0
    except: gold_val = 2300.0
    try: sil_val = float(hist_data["silver"].loc[idx]) if idx in hist_data["silver"].index else 28.0
    except: sil_val = 28.0
    try: plat_val = float(hist_data["platinum"].loc[idx]) if idx in hist_data["platinum"].index else 1000.0
    except: plat_val = 1000.0

    res["wti"].append({"date": date_str, "price": round(wti_val, 2)})
    res["copper"].append({"date": date_str, "price": round(cop * 2204.62, 1)})
    res["aluminum"].append({"date": date_str, "price": round(alu_val, 1)})
    res["gold"].append({"date": date_str, "price": round(gold_val, 2)})
    res["silver"].append({"date": date_str, "price": round(sil_val, 2)})
    res["platinum"].append({"date": date_str, "price": round(plat_val, 2)})
  return res

res_daily = generate_6_commodities_daily()

market_summary_for_ai = {
    "update_date": datetime.now().strftime("%Y-%m-%d"),
    "macro": {
        "dxy": round(float(hist_data["dxy"].iloc[-1]), 2) if not hist_data["dxy"].empty else 104.2,
        "us10y": round(float(hist_data["us10y"].iloc[-1]), 2) if not hist_data["us10y"].empty else 4.15,
        "usdcny": round(float(hist_data["usdcny"].iloc[-1]), 4) if not hist_data["usdcny"].empty else 7.23,
        "usdkrw": round(float(hist_data["usdkrw"].iloc[-1]), 1) if not hist_data["usdkrw"].empty else 1380.0
    },
    "history": {k: v[-36:] for k, v in res_daily.items()} 
}

prompt = f"""
당신은 글로벌 원자재 및 거시경제 퀀트 분석 수석 애널리스트입니다.
제공된 [시장 입력 데이터]와 아래의 [원자재별 핵심 원가/수급/정책 드라이버 정보]를 정밀 동조 분석하여,
6대 핵심 원자재(wti, copper, aluminum, gold, silver, platinum) 각각의 예측가와 시나리오, 요인지표(metrics), 과거 유사국면(analogs) 분석 데이터셋을 작성하세요.

[수급 및 1차 원가 선행 드라이버 (Cost & Demand Drivers)]
1. wti: Cost(OPEC+ 감산 쿼터, 미국 원유 리그 수) / Demand(정제 마진, 미국 원유 재고, 가동률) / Geopolitics(지정학, 비축유 방출)
2. copper: Cost(제련수수료 TC/RC, 광산 생산량) / Demand(재고량, AI 전력망 및 EV용 전선 수요) / Geopolitics(칠레/페루 파업)
3. aluminum: Cost(보크사이트 단가, 제련 에너지비) / Demand(자동차 차체, 태양광 프레임) / Geopolitics(EU CBAM 탄소세)
4. gold: Cost(실질금리 TIPS, 달러인덱스) / Demand(중앙은행 매수량, ETF 유입량) / Geopolitics(인플레이션 헤지, 전쟁 지정학)
5. silver: Cost(금/은 비율 Ratio, 실질금리) / Demand(태양광 페이스트, 반도체 부품) / Geopolitics(산업용 은 공급 부족)
6. platinum: Cost(남아공/러시아 공급원가) / Demand(하이브리드 촉매, 수소 연료전지) / Geopolitics(남아공 전력난)

[요인지표(metrics) 및 과거 유사국면(analogs) 미니 3선 그래프 구성 지침]
- 요인지표(metrics)에는 대상 지표의 이름, 값, 분류(cat: 수요/공급/투자/재고), 상태(status: 강세/약세/보통), 뱃지(badge: danger/primary/secondary)를 최소 5개 이상 담아주세요.
- Rationale(한줄 요약)에는 공급 요인의 칠레 구리 광산 생산량 감소 폭 확대와 수요 요인의 위안화 환율 하락이 가격 상방을 지지하고 있습니다 처럼 영향도를 기재해 주세요.
- 과거 유사국면(analogs)에는 과거 유사 국면 시점 '전후 12개월 추이'인 miniHist(숫자 12개)와 '이후 6개월 결과'인 miniForecast(숫자 6개)를 포함하세요.

[시장 입력 데이터]
{json.dumps(market_summary_for_ai, ensure_ascii=False)}

마크다운 없이 순수 JSON 포맷으로만 응답하세요. 스키마:
{{
  "update_date": "{market_summary_for_ai['update_date']}",
  "commodities": {{
    "wti": {{ 
      "name": "WTI 원유 (CME)", "unit": "USD/bbl", "current_price": 0.0, "forecast_6m_target": 0.0, "forecast_change_rate": "+0.0%", "volatility_score": 0, 
      "planning_advisor": "Planning Advisor : [전략 문구]",
      "monthly_forecast_base": [ {{"month": "2026-09", "price": 0.0, "rationale": "예측 근거"}} ],
      "monthly_forecast_bull": [ {{"month": "2026-09", "price": 0.0}} ],
      "monthly_forecast_bear": [ {{"month": "2026-09", "price": 0.0}} ],
      "rationale_base": "기본 요약", "rationale_bull": "낙관 요약", "rationale_bear": "비관 요약",
      "metrics": [ {{"label": "위안화 환율", "val": "6.72223 (USD/CNY)", "date": "2026.08.27", "cat": "수요", "status": "보통", "badge": "secondary"}} ],
      "analogs": [
        {{ "period": "2020.04 - 2021.03", "similarity": "92%", "acc": "92%", "forecast": "+14.9%", "actual": "+3.8%", "title": "비둘기파적 FOMC + LME 재고량 증가", "summary": "공급 측면의 구리 TC 하락이...",
          "miniHist": [5100, 5200, 5500, 5800, 6100, 6300, 6500, 6800, 7200, 7500, 8000, 8500],
          "miniForecast": [8800, 9000, 9200, 9400, 9600, 9800]
        }}
      ]
    }},
    "copper": {{ "name": "전기동 (LME)", "unit": "USD/ton", "current_price": 0.0, "forecast_6m_target": 0.0, "forecast_change_rate": "+0.0%", "volatility_score": 0, "planning_advisor": "Planning Advisor : [전략 문구]", "monthly_forecast_base": [ {{"month": "2026-09", "price": 0.0, "rationale": "근거"}} ], "monthly_forecast_bull": [ {{"month": "2026-09", "price": 0.0}} ], "monthly_forecast_bear": [ {{"month": "2026-09", "price": 0.0}} ], "rationale_base": "기본", "rationale_bull": "낙관", "rationale_bear": "비관", "metrics": [ {{"label": "구리 TC", "val": "-227.5 (USD/mt)", "date": "2026.08.27", "cat": "공급", "status": "강세", "badge": "danger"}} ], "analogs": [ {{ "period": "2020.04-2021.03", "similarity": "92%", "acc": "92%", "forecast": "+14.9%", "actual": "+3.8%", "title": "비둘기파적 FOMC", "summary": "요약", "miniHist": [5000,5200,5400,5600,5800,6000,6200,6400,6600,6800,7000,7200], "miniForecast": [7400,7600,7800,8000,8200,8400] }} ] }},
    "aluminum": {{ "name": "알루미늄 (LME)", "unit": "USD/ton", "current_price": 0.0, "forecast_6m_target": 0.0, "forecast_change_rate": "+0.0%", "volatility_score": 0, "planning_advisor": "Planning Advisor : [전략 문구]", "monthly_forecast_base": [ {{"month": "2026-09", "price": 0.0, "rationale": "근거"}} ], "monthly_forecast_bull": [ {{"month": "2026-09", "price": 0.0}} ], "monthly_forecast_bear": [ {{"month": "2026-09", "price": 0.0}} ], "rationale_base": "기본", "rationale_bull": "낙관", "rationale_bear": "비관", "metrics": [ {{"label": "제련 전력비", "val": "142.5 (EUR/MWh)", "date": "2026.08.27", "cat": "공급", "status": "보통", "badge": "secondary"}} ], "analogs": [ {{ "period": "2016.09-2017.08", "similarity": "86%", "acc": "98%", "forecast": "+8.3%", "actual": "+8.2%", "title": "중국 인프라 투자", "summary": "요약", "miniHist": [1800,1850,1900,1950,2000,2050,2100,2150,2200,2250,2300,2350], "miniForecast": [2400,2450,2500,2550,2600,2650] }} ] }},
    "gold": {{ "name": "금 (LBMA)", "unit": "USD/oz.t", "current_price": 0.0, "forecast_6m_target": 0.0, "forecast_change_rate": "+0.0%", "volatility_score": 0, "planning_advisor": "Planning Advisor : [전략 문구]", "monthly_forecast_base": [ {{"month": "2026-09", "price": 0.0, "rationale": "근거"}} ], "monthly_forecast_bull": [ {{"month": "2026-09", "price": 0.0}} ], "monthly_forecast_bear": [ {{"month": "2026-09", "price": 0.0}} ], "rationale_base": "기본", "rationale_bull": "낙관", "rationale_bear": "비관", "metrics": [ {{"label": "10년물 실질금리", "val": "1.85 (Percent)", "date": "2026.08.27", "cat": "투자", "status": "강세", "badge": "danger"}} ], "analogs": [ {{ "period": "2019.01-2019.12", "similarity": "88%", "acc": "95%", "forecast": "+11.5%", "actual": "+10.2%", "title": "금리동향", "summary": "요약", "miniHist": [1200,1250,1300,1350,1400,1450,1500,1550,1600,1650,1700,1750], "miniForecast": [1800,1850,1900,1950,2000,2050] }} ] }},
    "silver": {{ "name": "은 (LBMA)", "unit": "US￠/oz.t", "current_price": 0.0, "forecast_6m_target": 0.0, "forecast_change_rate": "+0.0%", "volatility_score": 0, "planning_advisor": "Planning Advisor : [전략 문구]", "monthly_forecast_base": [ {{"month": "2026-09", "price": 0.0, "rationale": "근거"}} ], "monthly_forecast_bull": [ {{"month": "2026-09", "price": 0.0}} ], "monthly_forecast_bear": [ {{"month": "2026-09", "price": 0.0}} ], "rationale_base": "기본", "rationale_bull": "낙관", "rationale_bear": "비관", "metrics": [ {{"label": "태양광용 은 수요", "val": "4250 (TONNES)", "date": "2026.08.27", "cat": "수요", "status": "강세", "badge": "danger"}} ], "analogs": [ {{ "period": "2019.01-2019.12", "similarity": "88%", "acc": "95%", "forecast": "+11.5%", "actual": "+10.2%", "title": "수요동향", "summary": "요약", "miniHist": [15,15.5,16,16.5,17,17.5,18,18.5,19,19.5,20,20.5], "miniForecast": [21,21.5,22,22.5,23,23.5] }} ] }},
    "platinum": {{ "name": "백금 (CME)", "unit": "USD/oz.t", "current_price": 0.0, "forecast_6m_target": 0.0, "forecast_change_rate": "+0.0%", "volatility_score": 0, "planning_advisor": "Planning Advisor : [전략 문구]", "monthly_forecast_base": [ {{"month": "2026-09", "price": 0.0, "rationale": "근거"}} ], "monthly_forecast_bull": [ {{"month": "2026-09", "price": 0.0}} ], "monthly_forecast_bear": [ {{"month": "2026-09", "price": 0.0}} ], "rationale_base": "기본", "rationale_bull": "낙관", "rationale_bear": "비관", "metrics": [ {{"label": "남아공 광산 원가", "val": "보통 (Level)", "date": "2026.08.27", "cat": "공급", "status": "강세", "badge": "danger"}} ], "analogs": [ {{ "period": "2019.01-2019.12", "similarity": "88%", "acc": "95%", "forecast": "+11.5%", "actual": "+10.2%", "title": "남아공전력난", "summary": "요약", "miniHist": [800,830,850,880,900,920,950,980,1000,1020,1050,1080], "miniForecast": [1100,1120,1150,1180,1200,1220] }} ] }}
  }}
}}
"""

print("[진행] Gemini 다변량 퀀트 오버레이 패턴 매칭 분석 가동...")

# 구글이 제공한 최신 신규 유저용 강제 마일스톤 모델명으로 정적 대체
MAX_RETRIES = 5
MODELS_TO_TRY = ["gemini-3.6-flash", "gemini-3.1-pro-preview"]
response = None
last_exception = None
success = False

for model_name in MODELS_TO_TRY:
  try:
    print(f"[진행] {model_name} 연산 시도 중...")
    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
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

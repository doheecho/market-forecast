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

# 2. 다변량 핵심 시장 데이터 수집 (야후 파이낸스 실거래 6대 품목, 월단위 interval="1mo")
TICKERS = {
    "copper": "HG=F",      # 구리 (USD/lb) -> LME 톤단가 변환용
    "aluminum": "ALI=F",   # LME 알루미늄 (USD/mt)
    "wti": "CL=F",         # CME WTI 원유 (USD/bbl)
    "gold": "GC=F",        # LBMA 금 (USD/oz.t)
    "silver": "SI=F",      # LBMA 은 (US￠/oz.t)
    "platinum": "PL=F",    # CME 백금 (USD/oz.t)
    "dxy": "DX-Y.NYB",     # 달러 인덱스
    "us10y": "^TNX",       # 미국 10년물 국채금리
    "usdcny": "CNY=X",     # 위안화 환율
    "usdkrw": "KRW=X"      # 원/달러 환율
}

print("[진행] 야후 파이낸스에서 3년 월단위 시계열 데이터 수집 중...")
hist_data = {}
for name, ticker in TICKERS.items():
  try:
    df = yf.Ticker(ticker).history(period="3y", interval="1mo")["Close"]
    df = df.dropna()
    hist_data[name] = df
  except Exception as e:
    print(f"[경고] {name}({ticker}) 수집 실패: {e}")
    hist_data[name] = pd.Series(dtype=float)


# 6대 원자재 월 단위 가격 가공 함수
def generate_6_commodities():
  pivot_series = hist_data.get("copper", pd.Series())
  if pivot_series.empty:
    pivot_series = hist_data.get("wti", pd.Series())

  res = {k: [] for k in ["wti", "copper", "aluminum", "gold", "silver", "platinum"]}

  for idx, val in pivot_series.items():
    date_str = idx.strftime("%Y-%m")

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


# 월간 AI용 요약 패킷 가공
res_m = generate_6_commodities()

market_summary_for_ai = {
    "update_date": datetime.now().strftime("%Y-%m-%d"),
    "macro": {
        "dxy": round(float(hist_data["dxy"].iloc[-1]), 2) if not hist_data["dxy"].empty else 104.2,
        "us10y": round(float(hist_data["us10y"].iloc[-1]), 2) if not hist_data["us10y"].empty else 4.15,
        "usdcny": round(float(hist_data["usdcny"].iloc[-1]), 4) if not hist_data["usdcny"].empty else 7.23,
        "usdkrw": round(float(hist_data["usdkrw"].iloc[-1]), 1) if not hist_data["usdkrw"].empty else 1380.0
    },
    "history": res_m,
}

# 3. 고도화된 Gemini 프롬프트 구성 (요인지표 및 과거 유사국면 정성/정량 데이터의 실시간 동적 분석 지시)
prompt = f"""
당신은 글로벌 원자재 및 거시경제 퀀트 분석 수석 애널리스트입니다.
아래 제공된 [3년간의 월별 과거 시계열 데이터]와 [거시경제 지표]를 종합하여,
6대 실거래 원자재 각각에 대한 'Planning Advisor 추천 전략 요약 멘트', '향후 6개월 월별 정밀 예측가 및 고유 예측근거', '수급 요인지표 분석 목록', '과거 유사국면 분석 목록'을 독립적으로 분석 작성하세요.

[분석 타깃 품목]
1. wti: WTI 원유 (bbl)
2. copper: 전기동 (LME/ton)
3. aluminum: 알루미늄 (LME/ton)
4. gold: 금 (LBMA/oz.t)
5. silver: 은 (LBMA/US￠/oz.t)
6. platinum: 백금 (CME/oz.t)

[요인지표(metrics) 수합 분석 가이드라인]
- 해당 품목 시황에 직/간접 영향을 미치는 글로벌 지표(환율, 금리, LME 재고량, 광산 수급 등)를 최소 4개 이상 분석해 JSON 목록에 담으세요.
- cat(분주): 수요, 공급, 투자, 재고 중 택일.
- status(상태): 강세, 약세, 보통 중 택일.
- badge: 강세면 danger, 약세면 primary, 보통이면 secondary 중 택일.

[과거 유사국면(analogs) 분석 가이드라인]
- 최근 3개년 가격 등락 파동과 역사적으로 가장 유사한 흐름을 보였던 과거 유사 국면을 최소 1개 이상 탐색/선정하세요.
- title: 국면의 핵심 매크로 배경 제목 (예: "비둘기파적 FOMC + LME 재고량 증가")
- summary: 해당 시기의 구체적인 원자재 수급/거시 동조화 분석 요약 (3문장)
- miniHist: 과거 해당 국면의 12개월 월별 가격 흐름 목록 (숫자 배열)
- miniForecast: 그 이후 실제 진행된 6개월의 가격 흐름 목록 (숫자 배열)

[시장 입력 데이터]
{json.dumps(market_summary_for_ai, ensure_ascii=False)}

반드시 마크다운 없이 순수 JSON 포맷으로만 응답하세요. 스키마:
{{
  "update_date": "{market_summary_for_ai['update_date']}",
  "commodities": {{
    "wti": {{ 
      "name": "WTI 원유 (CME)", 
      "unit": "USD/bbl", 
      "current_price": 0.0, 
      "forecast_6m_target": 0.0, 
      "forecast_change_rate": "+0.0%", 
      "direction": "상승/하락/보합", 
      "volatility_score": 0, 
      "planning_advisor": "Planning Advisor : WTI 유가는 향후 6개월간 약 0.0% 변동할 전망입니다. [최적 구매 및 물량 대응 전략 1문장 추가]",
      "monthly_forecast": [
        {{"month": "2026-09", "price": 0.0, "rationale": "9월 수급 및 정밀 예측 고유 근거"}}, 
        {{"month": "2026-10", "price": 0.0, "rationale": "10월 가격 변동 고유 근거"}}, 
        {{"month": "2026-11", "price": 0.0, "rationale": "11월 거시 매크로 예측 고유 근거"}}, 
        {{"month": "2026-12", "price": 0.0, "rationale": "12월 재고 동향 예측 고유 근거"}}, 
        {{"month": "2027-01", "price": 0.0, "rationale": "1월 아시아 수요 예측 고유 근거"}}, 
        {{"month": "2027-02", "price": 0.0, "rationale": "2월 계절 조율 예측 고유 근거"}}
      ],
      "rationale_base": "기본 시나리오 요약 근거 (1문장)",
      "rationale_bull": "낙관 시나리오 요약 근거 및 확률 25% (1문장)",
      "rationale_bear": "비관 시나리오 요약 근거 및 확률 25% (1문장)",
      "monthly_forecast_base": [ {{"month": "2026-09", "price": 0.0}}, ... ],
      "monthly_forecast_bull": [ {{"month": "2026-09", "price": 0.0}}, ... ],
      "monthly_forecast_bear": [ {{"month": "2026-09", "price": 0.0}}, ... ],
      "metrics": [
        {{"label": "지표명", "val": "수치 및 단위", "date": "2026.08.27", "cat": "수요", "status": "보통", "badge": "secondary"}}
      ],
      "analogs": [
        {{
          "period": "2020.04 - 2021.03",
          "similarity": "92%",
          "acc": "92%",
          "forecast": "+14.9%",
          "actual": "+3.8%",
          "title": "비둘기파적 FOMC + LME 재고량 증가",
          "summary": "공급 측면의 구리 TC 하락이...",
          "miniHist": [5100, 5200, 5500, 5800, 6100, 6300, 6500, 6800, 7200, 7500, 8000, 8500],
          "miniForecast": [8800, 9000, 9200, 9400, 9600, 9800]
        }}
      ]
    }},
    "copper": {{ "name": "전기동 (LME)", ... }},
    "aluminum": {{ ... }},
    "gold": {{ ... }},
    "silver": {{ ... }},
    "platinum": {{ ... }}
  }}
}}
"""

print("[진행] Gemini 3.6/2.5/1.5 다변량 시계열 3대 시나리오 정밀 연산 수행 중...")

# 다중 모델 폴백 및 지수 백오프 자동 재시도 시스템 구축 (503 UNAVAILABLE 완벽 대응)
MAX_RETRIES = 5
MODELS_TO_TRY = ["gemini-3.6-flash", "gemini-2.5-flash", "gemini-1.5-flash"]
response = None
last_exception = None
success = False

for model_name in MODELS_TO_TRY:
  print(f"[진행] {model_name} 모델로 원자재 3대 시나리오 예측 시도 중...")
  for attempt in range(1, MAX_RETRIES + 1):
    try:
      response = client.models.generate_content(
          model=model_name,
          contents=prompt,
          config=types.GenerateContentConfig(
              response_mime_type="application/json",
          ),
      )
      print(f"[성공] {model_name} 연산 완료 (시도 {attempt}회째)")
      success = True
      break
    except Exception as e:
      last_exception = e
      # 일시적 503 대응 지수 백오프
      wait_time = 2**attempt
      print(
          f"[경고] {model_name} {attempt}/{MAX_RETRIES} 실패 (에러: {e})."
          f" {wait_time}초 후 재시도..."
      )
      time.sleep(wait_time)

  if success:
    break
else:
  print(f"[최종 에러] 모든 가용 가능한 Gemini 모델 연산 실패: {last_exception}")
  exit(1)

try:
  result_json = json.loads(response.text)

  # 데이터 결합 (과거도 동일한 월단위 수집 데이터셋 반영)
  final_output = {
      "update_date": market_summary_for_ai["update_date"],
      "macro": market_summary_for_ai["macro"],
      "history_3y": res_m,
      "forecast_data": result_json["commodities"],
  }

  with open("raw_materials_forecast.json", "w", encoding="utf-8") as f:
    json.dump(final_output, f, ensure_ascii=False, indent=2)

  print(
      f"[성공] raw_materials_forecast.json 생성 완료:"
      f" {market_summary_for_ai['update_date']} (Planning Advisor, 수급지표, 유사국면 동적분석 이식 완수)"
  )

except Exception as e:
  print(f"[JSON 파싱 및 쓰기 실패] {e}")
  exit(1)

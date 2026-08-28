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

# 2. 다변량 핵심 시장 데이터 수집 (실거래 6대 품목, 월단위 interval="1mo")
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
    # 차트 가로폭 대칭화를 위해 월 단위 interval="1mo" 로 원복 수집
    df = yf.Ticker(ticker).history(period="3y", interval="1mo")["Close"]
    df = df.dropna()
    hist_data[name] = df
  except Exception as e:
    print(f"[경고] {name}({ticker}) 수집 실패: {e}")
    hist_data[name] = pd.Series(dtype=float)


# 6대 원자재 월 단위 가격 생성 함수
def generate_6_commodities():
  pivot_series = hist_data.get("copper", pd.Series())
  if pivot_series.empty:
    pivot_series = hist_data.get("wti", pd.Series())

  res = {k: [] for k in ["wti", "copper", "aluminum", "gold", "silver", "platinum"]}

  for idx, val in pivot_series.items():
    date_str = idx.strftime("%Y-%m")

    try:
      cop = float(val)
    except:
      cop = 4.0

    try:
      wti_val = float(hist_data["wti"].loc[idx]) if idx in hist_data["wti"].index else 75.0
    except:
      wti_val = 75.0

    try:
      alu_val = float(hist_data["aluminum"].loc[idx]) if idx in hist_data["aluminum"].index else 2200.0
    except:
      alu_val = 2200.0

    try:
      gold_val = float(hist_data["gold"].loc[idx]) if idx in hist_data["gold"].index else 2300.0
    except:
      gold_val = 2300.0

    try:
      sil_val = float(hist_data["silver"].loc[idx]) if idx in hist_data["silver"].index else 28.0
    except:
      sil_val = 28.0

    try:
      plat_val = float(hist_data["platinum"].loc[idx]) if idx in hist_data["platinum"].index else 1000.0
    except:
      plat_val = 1000.0

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
    "history": res_m,
}

# 3. 고도화된 Gemini 프롬프트 구성 (Planning Advisor 및 월별 순차적 고유 예측 근거 강제 수집)
prompt = f"""
당신은 글로벌 원자재 및 거시경제 퀀트 분석 수석 애널리스트입니다.
아래 제공된 [3년간의 월별 과거 시계열 데이터]와 [거시경제 지표]를 종합하여,
6대 실거래 원자재 각각에 대한 'Planning Advisor 추천 전략 요약 멘트'와 '향후 6개월간(M+1 ~ M+6)의 월별 가격 예측 및 월별 순차 고유 예측 근거'를 작성하세요.

[분석 타깃 품목]
1. wti: WTI 원유 (bbl)
2. copper: 전기동 (LME/ton)
3. aluminum: 알루미늄 (LME/ton)
4. gold: 금 (LBMA/oz.t)
5. silver: 은 (LBMA/US￠/oz.t)
6. platinum: 백금 (CME/oz.t)

[추천 전략 멘트 (planning_advisor) 가이드라인]
- 해당 품목의 향후 6개월 가격 등락률 전망과 함께, 구매 부서 입장에서 언제 매입하고 물량을 얼마나 확보해야 하는지 최적의 구매 의사결정 전략을 전문적인 2문장 볼드체 멘트로 요약 작성하세요.
- 예시: "전기동 가격은 향후 6개월간 약 13.3% 상승할 전망입니다. 6개월 내 가격이 급등하여 16,000달러 선 돌파 가능성이 있으니 구매 시점과 물량 확보 전략을 최적화하시기 바랍니다."

[월별 세부 예측 근거 가이드라인]
- 매월 예측되는 가격에 맞춰, 해당 월에 발생할 구체적인 시황 및 수요/공급 변동성 요인을 월별로 **완전하게 다른 고유한 내용**으로 1~2문장 기술하세요 (매월 내용이 절대 중복되거나 일률적이지 않아야 합니다).

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
      "planning_advisor": "6개월 가격 등락률 전망 및 추천 구매 시점/물량 확보 가이드 요약 (전문적 2문장 볼드체)",
      "monthly_forecast": [
        {{"month": "2026-09", "price": 0.0, "rationale": "9월 시황 수급 요인 및 예측 가격 도출의 구체적 고유 근거 (내용 중복 엄금)"}}, 
        {{"month": "2026-10", "price": 0.0, "rationale": "10월 시황 공급망 병목 및 정밀 예측 고유 근거"}}, 
        {{"month": "2026-11", "price": 0.0, "rationale": "11월 미국 대선 및 달러화 경로 연계 예측 고유 근거"}}, 
        {{"month": "2026-12", "price": 0.0, "rationale": "12월 연말 계절적 난방 수요 및 재고 추이 예측 고유 근거"}}, 
        {{"month": "2027-01", "price": 0.0, "rationale": "1월 아시아 실물 소비 및 연초 생산 가동 예측 고유 근거"}}, 
        {{"month": "2027-02", "price": 0.0, "rationale": "2월 중국 춘절 연휴 및 계절적 비성기 공급 조율 예측 고유 근거"}}
      ]
    }},
    "copper": {{ "name": "전기동 (LME)", "unit": "USD/ton", "current_price": 0.0, "forecast_6m_target": 0.0, "forecast_change_rate": "+0.0%", "direction": "상승/하락/보합", "volatility_score": 0, "planning_advisor": "...", "monthly_forecast": [...] }},
    "aluminum": {{ ... }},
    "gold": {{ ... }},
    "silver": {{ ... }},
    "platinum": {{ ... }}
  }}
}}
"""

print("[진행] Gemini 3.6/2.5/1.5 다변량 시계열 정밀 연산 수행 중...")

# 다중 모델 폴백 및 지수 백오프 자동 재시도 시스템 구축 (503 UNAVAILABLE 완벽 대응)
MAX_RETRIES = 5
MODELS_TO_TRY = ["gemini-3.6-flash", "gemini-2.5-flash", "gemini-1.5-flash"]
response = None
last_exception = None
success = False

for model_name in MODELS_TO_TRY:
  print(f"[진행] {model_name} 모델로 원자재 기획 예측 시도 중...")
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
      f" {market_summary_for_ai['update_date']} (Planning Advisor 및 월별 순차적 고유근거 탑재 버전)"
  )

except Exception as e:
  print(f"[JSON 파싱 및 쓰기 실패] {e}")
  exit(1)

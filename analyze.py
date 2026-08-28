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

# 2. 다변량 핵심 시장 데이터 수집 (글로벌 벤치마크 10대 품목)
TICKERS = {
    "copper": "HG=F",       # 구리 (USD/lb) -> LME 톤단가 변환용
    "aluminum": "ALI=F",    # LME 알루미늄 (USD/mt)
    "wti": "CL=F",          # CME WTI 원유 (USD/bbl)
    "gold": "GC=F",         # LBMA 금 (USD/oz.t)
    "silver": "SI=F",       # LBMA 은 (US￠/oz.t)
    "platinum": "PL=F",     # CME 백금 (USD/oz.t)
    "zinc": "ZINC",         # WisdomTree Zinc ETF (LME 아연 싱크)
    "nickel": "DBB",        # Invesco Base Metals ETF (LME 니켈/구리/아연 복합 비철 지표)
    "rare_earth": "REMX",   # VanEck Strategic Metals ETF (텅스텐 등 희유금속 글로벌 지표)
    "steel": "SLX",         # VanEck Steel ETF (철강 완제품 글로벌 지표)
    "dxy": "DX-Y.NYB",      # 달러 인덱스
    "us10y": "^TNX",        # 미국 10년물 국채금리
    "usdcny": "CNY=X",      # 위안화 환율
    "usdkrw": "KRW=X"       # 원/달러 환율
}

print("[진행] 야후 파이낸스에서 3년 일단위 시계열 데이터 수집 중...")
hist_data = {}
for name, ticker in TICKERS.items():
  try:
    df = yf.Ticker(ticker).history(period="3y", interval="1d")["Close"]
    df = df.dropna()
    hist_data[name] = df
  except Exception as e:
    print(f"[경고] {name}({ticker}) 수집 실패: {e}")
    hist_data[name] = pd.Series(dtype=float)


# 10대 글로벌 핵심 원자재 가격 생성 함수 (일단위 및 월단위)
def generate_10_commodities(is_daily=True):
  pivot_series = hist_data.get("copper", pd.Series())
  if pivot_series.empty:
    pivot_series = hist_data.get("wti", pd.Series())

  res = {
      k: []
      for k in [
          "wti", "copper", "aluminum", "gold", "silver", 
          "platinum", "zinc", "nickel", "tungsten", "steel"
      ]
  }

  for idx, val in pivot_series.items():
    if is_daily:
      date_str = idx.strftime("%Y-%m-%d")
    else:
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

    try:
      zinc_val = float(hist_data["zinc"].loc[idx]) if idx in hist_data["zinc"].index else 25.0
    except:
      zinc_val = 25.0

    try:
      nick_val = float(hist_data["nickel"].loc[idx]) if idx in hist_data["nickel"].index else 20.0
    except:
      nick_val = 20.0

    try:
      rare_val = float(hist_data["rare_earth"].loc[idx]) if idx in hist_data["rare_earth"].index else 80.0
    except:
      rare_val = 80.0

    try:
      steel_val = float(hist_data["steel"].loc[idx]) if idx in hist_data["steel"].index else 100.0
    except:
      steel_val = 100.0

    # 10대 실거래 원자재 가치 가공 적용
    res["wti"].append({"date": date_str, "price": round(wti_val, 2)})
    res["copper"].append({"date": date_str, "price": round(cop * 2204.62, 1)})
    res["aluminum"].append({"date": date_str, "price": round(alu_val, 1)})
    res["gold"].append({"date": date_str, "price": round(gold_val, 2)})
    res["silver"].append({"date": date_str, "price": round(sil_val, 2)})
    res["platinum"].append({"date": date_str, "price": round(plat_val, 2)})
    res["zinc"].append({"date": date_str, "price": round(zinc_val, 2)})
    res["nickel"].append({"date": date_str, "price": round(nick_val, 2)})
    res["tungsten"].append({"date": date_str, "price": round(rare_val, 2)})
    res["steel"].append({"date": date_str, "price": round(steel_val, 2)})

  return res


# pandas ME와 M 호환 다운샘플링용 헬퍼
def format_history_monthly_resample(series):
  try:
    return series.resample("ME").last().dropna()
  except ValueError:
    return series.resample("M").last().dropna()


# 월간 AI용 요약 패킷 가공
cop_m = format_history_monthly_resample(hist_data.get("copper", pd.Series()))
wti_m = format_history_monthly_resample(hist_data.get("wti", pd.Series()))
alu_m = format_history_monthly_resample(hist_data.get("aluminum", pd.Series()))
gold_m = format_history_monthly_resample(hist_data.get("gold", pd.Series()))
sil_m = format_history_monthly_resample(hist_data.get("silver", pd.Series()))
plat_m = format_history_monthly_resample(hist_data.get("platinum", pd.Series()))
zinc_m = format_history_monthly_resample(hist_data.get("zinc", pd.Series()))
nick_m = format_history_monthly_resample(hist_data.get("nickel", pd.Series()))
rare_m = format_history_monthly_resample(hist_data.get("rare_earth", pd.Series()))
steel_m = format_history_monthly_resample(hist_data.get("steel", pd.Series()))

res_m = {
    k: []
    for k in [
        "wti", "copper", "aluminum", "gold", "silver", 
        "platinum", "zinc", "nickel", "tungsten", "steel"
    ]
}
pivot_m = cop_m if not cop_m.empty else wti_m

for idx, val in pivot_m.items():
  date_str = idx.strftime("%Y-%m")
  cop = float(val) if not pivot_m.empty else 4.0
  wti_val = float(wti_m.loc[idx]) if idx in wti_m.index else 75.0
  alu_val = float(alu_m.loc[idx]) if idx in alu_m.index else 2200.0
  gold_val = float(gold_m.loc[idx]) if idx in gold_m.index else 2300.0
  sil_val = float(sil_m.loc[idx]) if idx in sil_m.index else 28.0
  plat_val = float(plat_m.loc[idx]) if idx in plat_m.index else 1000.0
  zinc_val = float(zinc_m.loc[idx]) if idx in zinc_m.index else 25.0
  nick_val = float(nick_m.loc[idx]) if idx in nick_m.index else 20.0
  rare_val = float(rare_m.loc[idx]) if idx in rare_m.index else 80.0
  steel_val = float(steel_m.loc[idx]) if idx in steel_m.index else 100.0

  res_m["wti"].append({"date": date_str, "price": round(wti_val, 2)})
  res_m["copper"].append({"date": date_str, "price": round(cop * 2204.62, 1)})
  res_m["aluminum"].append({"date": date_str, "price": round(alu_val, 1)})
  res_m["gold"].append({"date": date_str, "price": round(gold_val, 2)})
  res_m["silver"].append({"date": date_str, "price": round(sil_val, 2)})
  res_m["platinum"].append({"date": date_str, "price": round(plat_val, 2)})
  res_m["zinc"].append({"date": date_str, "price": round(zinc_val, 2)})
  res_m["nickel"].append({"date": date_str, "price": round(nick_val, 2)})
  res_m["tungsten"].append({"date": date_str, "price": round(rare_val, 2)})
  res_m["steel"].append({"date": date_str, "price": round(steel_val, 2)})

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

# 3. 고도화된 Gemini 프롬프트 구성 (6개월 월별 세부 예측 근거 스키마 정렬 추가)
prompt = f"""
당신은 글로벌 원자재 및 거시경제 퀀트 분석 수석 애널리스트입니다.
아래 제공된 [3년간의 월별 과거 시계열 데이터]와 [거시경제 지표]를 종합하여,
10대 대표 실거래 원자재 각각에 대한 '향후 6개월간(M+1 ~ M+6)의 정밀 가격 예측'과 '전체 산정 근거 요약', 그리고 '월별 세부 예측 근거'를 함께 작성하세요.

[분석 타깃 품목]
1. wti: WTI 원유 (bbl)
2. copper: 전기동 (LME/ton)
3. aluminum: 알루미늄 (LME/ton)
4. gold: 금 (LBMA/oz.t)
5. silver: 은 (LBMA/US￠/oz.t)
6. platinum: 백금 (CME/oz.t)
7. zinc: 아연 (LME 대리 ZINC ETF/share)
8. nickel: 니켈 (LME 대리 DBB ETF/share)
9. tungsten: 텅스텐/희유금속 (대리 REMX ETF/share)
10. steel: 철강 완제품 (대리 SLX ETF/share)

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
      "rationale": "전체 산정 근거 요약 (3~4문장)", 
      "monthly_forecast": [
        {{"month": "2026-09", "price": 0.0}}, 
        {{"month": "2026-10", "price": 0.0}}, 
        {{"month": "2026-11", "price": 0.0}}, 
        {{"month": "2026-12", "price": 0.0}}, 
        {{"month": "2027-01", "price": 0.0}}, 
        {{"month": "2027-02", "price": 0.0}}
      ],
      "monthly_rationales": [
        {{"month": "2026-09", "rationale": "9월 시황 및 정밀 가격 예측 근거 (1~2문장)"}},
        {{"month": "2026-10", "rationale": "10월 시황 및 정밀 가격 예측 근거 (1~2문장)"}},
        {{"month": "2026-11", "rationale": "11월 시황 및 정밀 가격 예측 근거 (1~2문장)"}},
        {{"month": "2026-12", "rationale": "12월 시황 및 정밀 가격 예측 근거 (1~2문장)"}},
        {{"month": "2027-01", "rationale": "1월 시황 및 정밀 가격 예측 근거 (1~2문장)"}},
        {{"month": "2027-02", "rationale": "2월 시황 및 정밀 가격 예측 근거 (1~2문장)"}}
      ]
    }},
    "copper": {{ "name": "전기동 (LME)", "unit": "USD/ton", ... }},
    "aluminum": {{ ... }},
    "gold": {{ ... }},
    "silver": {{ ... }},
    "platinum": {{ ... }},
    "zinc": {{ ... }},
    "nickel": {{ ... }},
    "tungsten": {{ ... }},
    "steel": {{ ... }}
  }}
}}
"""

print("[진행] Gemini 3.6/2.5/1.5 다변량 시나리오 연산 수행 중...")

# 다중 모델 폴백 및 지수 백오프 자동 재시도 시스템 구축 (503 UNAVAILABLE 완벽 대응)
MAX_RETRIES = 5
MODELS_TO_TRY = ["gemini-3.6-flash", "gemini-2.5-flash", "gemini-1.5-flash"]
response = None
last_exception = None
success = False

for model_name in MODELS_TO_TRY:
  print(f"[진행] {model_name} 모델로 원자재 예측 시도 중...")
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

  # 일간 10종 정밀 시계열 생성
  history_daily_out = generate_10_commodities(is_daily=True)

  # 데이터 결합
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
      f" {market_summary_for_ai['update_date']} (글로벌 실거래 10대 품목 및 월별 타임스넉 전망 탑재)"
  )

except Exception as e:
  print(f"[JSON 파싱 및 쓰기 실패] {e}")
  exit(1)

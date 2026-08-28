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

# 2. 다변량 촘촘한 일단위 데이터 수집 (과거 트렌드 노이즈 완벽 보존용, interval="1d")
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


# 6대 원자재 일 단위 가격 가공 함수
def generate_6_commodities_daily():
  pivot_series = hist_data.get("copper", pd.Series())
  if pivot_series.empty:
    pivot_series = hist_data.get("wti", pd.Series())

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

# 3. 고도화된 수급 원가 선행 드라이버 및 매크로 지표 마스터 프레임 워크 주입
prompt = f"""
당신은 글로벌 원자재 및 거시경제 퀀트 분석 수석 애널리스트입니다.
제공된 [시장 입력 데이터]와 아래의 [원자재별 핵심 원가/수급/정책 드라이버 정보]를 정밀 동조 분석하여,
6대 핵심 원자재별 향후 6개월 예측가와 시나리오, 요인지표(metrics), 과거 유사국면(analogs) 분석 데이터셋을 작성하세요.

[수급 및 1차 원가 선행 드라이버 (Cost & Demand Drivers)]
1. wti: Cost(OPEC+ 감산 쿼터, 미국 원유 리그 수) / Demand(정제 마진, 미국 원유 재고, 정제소 가동률) / Geopolitics(지정학, 비축유 방출)
2. copper: Cost(구리 정광 제련수수료 TC/RC, 광산 생산량) / Demand(LME/SHFE 재고량, AI 데이터센터 및 전력망 전선 수요) / Geopolitics(칠레/페루 파업, 물류 차질)
3. aluminum: Cost(보크사이트 단가, 제련 에너지 전력비) / Demand(자동차 차체, 태양광 프레임) / Geopolitics(EU CBAM 탄소세, 러시아 제재)
4. gold: Cost(미국 10년물 실질금리 TIPS, 달러인덱스) / Demand(중앙은행 매수량, ETF 유입량) / Geopolitics(인플레이션 헤지, 지정학 전쟁)
5. silver: Cost(금 가격비 Gold/Silver Ratio, 실질금리) / Demand(태양광 페이스트, 반도체 부품) / Geopolitics(산업용 은 공급 부족)
6. platinum: Cost(남아공/러시아 광산 공급 원가) / Demand(하이브리드 촉매, 수소 연료전지) / Geopolitics(남아공 전력난)

[매크로 및 물류 공통 참조치]
- 달러인덱스(DX-Y.NYB), 원달러환율(KRW=X), 위안화환율(CNY=X), 미국 국채금리(^TNX)
- 글로벌 제조업 PMI (미국 ISM, 중국 차이신 등) 및 OECD 경기선행지수 (CLI)
- 해상 운임: SCFI, BDI 벌크선 지수
- 투기 자금: CFTC 순매수 포지션 지수

[요인지표(metrics) 미래 전망 지침]
- 각 품목당 관련 지표 최소 5개 이상 구성.
- 공급 요인의 증가/감소, 수요 요인의 환율 등락에 따른 가격 상방/하방 영향도를 종합 분석하여 Rationale(한줄 요약)에 반드시 담아주세요.

[과거 유사국면(analogs) 미니 3선 그래프 구성 지침]
- 현재 시황과 가장 높은 유사도(%)를 가진 매칭 기간을 선정하세요.
- miniHist: 과거 유사 국면 당시의 12개월 연속 가격 궤적 (숫자 12개)
- miniForecast: 과거 유사 국면 시점 '이후' 실제 진행된 6개월의 가격 결과 궤적 (숫자 6개)
- [대조성 보장]: 클라이언트가 이 miniHist, miniForecast와 AI가 도출한 현재 시점 이후 미래 6개월 전망가('monthly_forecast_base')를 오버레이 대조하게 됩니다. 가격 스케일이 상호 매칭되도록 구성하세요.

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
      "volatility_score": 0, 
      "planning_advisor": "Planning Advisor : [요약 권고 문구]",
      "monthly_forecast_base": [
        {{"month": "2026-09", "price": 0.0, "rationale": "9월 수급 예측 근거"}}
      ],
      "monthly_forecast_bull": [
        {{"month": "2026-09", "price": 0.0}}
      ],
      "monthly_forecast_bear": [
        {{"month": "2026-09", "price": 0.0}}
      ],
      "rationale_base": "기본 시나리오 요약 근거 (1문장)",
      "rationale_bull": "낙관 시나리오 요약 근거 (1문장)",
      "rationale_bear": "비관 시나리오 요약 근거 (1문장)",
      "metrics": [
        {{"label": "위안화 환율", "val": "6.72223 (USD/CNY)", "date": "2026.08.27", "cat": "수요", "status": "보통", "badge": "secondary"}}
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

print("[진행] Gemini 다변량 퀀트 오버레이 패턴 매칭 분석 가동...")

MAX_RETRIES = 5
MODELS_TO_TRY = ["gemini-3.6-flash", "gemini-2.5-flash", "gemini-1.5-flash"]
response = None
last_exception = None
success = False

for model_name in MODELS_TO_TRY:
  try:
    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )
    success = True
    break
  except Exception as e:
    last_exception = e
    print(f"[경고] {model_name} 실패, 다음 모델 폴백 시도...")
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

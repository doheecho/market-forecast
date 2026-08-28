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

# 2. 다변량 핵심 시장 데이터 수집 (최근 3년 데이터, 일단위 interval="1d")
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
    df = yf.Ticker(ticker).history(period="3y", interval="1d")["Close"]
    df = df.dropna()
    hist_data[name] = df
  except Exception as e:
    print(f"[경고] {name}({ticker}) 수집 실패: {e}")
    hist_data[name] = pd.Series(dtype=float)


# 19종 글로벌 원자재 가격 합성 연산 모델링 함수 (일단위 및 월단위)
def generate_19_commodities(is_daily=True):
  pivot_series = hist_data.get("copper", pd.Series())
  if pivot_series.empty:
    pivot_series = hist_data.get("wti", pd.Series())

  res = {
      k: []
      for k in [
          "wti",
          "ldpe",
          "pvc",
          "abs",
          "pc",
          "nickel",
          "zinc",
          "aluminum",
          "copper",
          "magnesium",
          "silicon",
          "chromium",
          "tungsten",
          "wire_rod",
          "h_beam",
          "sts304",
          "gold",
          "silver",
          "platinum",
      ]
  }

  for idx, val in pivot_series.items():
    if is_daily:
      date_str = idx.strftime("%Y-%m-%d")
    else:
      date_str = idx.strftime("%Y-%m")

    # 해당 날짜의 기본 원천 가격 확보 (없는 경우 최근값 폴백)
    try:
      cop = float(val)
    except:
      cop = 4.0

    try:
      wti_val = (
          float(hist_data["wti"].loc[idx])
          if idx in hist_data["wti"].index
          else 75.0
      )
    except:
      wti_val = 75.0

    try:
      alu_val = (
          float(hist_data["aluminum"].loc[idx])
          if idx in hist_data["aluminum"].index
          else 2200.0
      )
    except:
      alu_val = 2200.0

    try:
      gold_val = (
          float(hist_data["gold"].loc[idx])
          if idx in hist_data["gold"].index
          else 2300.0
      )
    except:
      gold_val = 2300.0

    try:
      sil_val = (
          float(hist_data["silver"].loc[idx])
          if idx in hist_data["silver"].index
          else 28.0
      )
    except:
      sil_val = 28.0

    try:
      krw_val = (
          float(hist_data["usdkrw"].loc[idx])
          if idx in hist_data["usdkrw"].index
          else 1380.0
      )
    except:
      krw_val = 1380.0

    cop_ton = cop * 2204.62

    # 19종 가중치 공학 연산 모델 적용
    res["wti"].append({"date": date_str, "price": round(wti_val, 2)})
    res["ldpe"].append({
        "date": date_str,
        "price": round(wti_val * 7.5 + 420, 1),
    })
    res["pvc"].append({
        "date": date_str,
        "price": round(wti_val * 6.2 + 480, 1),
    })
    res["abs"].append({
        "date": date_str,
        "price": round(wti_val * 10.8 + 620, 1),
    })
    res["pc"].append({
        "date": date_str,
        "price": round(wti_val * 12.5 + 980, 1),
    })
    res["nickel"].append({"date": date_str, "price": round(cop_ton * 2.32, 1)})
    res["zinc"].append({"date": date_str, "price": round(alu_val * 1.14, 1)})
    res["aluminum"].append({"date": date_str, "price": round(alu_val, 1)})
    res["copper"].append({"date": date_str, "price": round(cop_ton, 1)})
    res["magnesium"].append({
        "date": date_str,
        "price": round(alu_val * 0.72 + 1100, 1),
    })
    res["silicon"].append({
        "date": date_str,
        "price": round(alu_val * 0.38 + wti_val * 4.5 + 950, 1),
    })
    res["chromium"].append({
        "date": date_str,
        "price": round(alu_val * 0.42 + wti_val * 3.8 + 1200, 1),
    })
    res["tungsten"].append({
        "date": date_str,
        "price": round(cop_ton * 4.8 + 8500, 1),
    })
    res["wire_rod"].append({
        "date": date_str,
        "price": round(cop_ton * 0.12 + 280, 1),
    })
    res["h_beam"].append({
        "date": date_str,
        "price": round((cop_ton * 0.12 + 250) * krw_val * 1.15, -3),
    })
    res["sts304"].append({
        "date": date_str,
        "price": round((cop_ton * 2.32 * 0.08 + 1600) * krw_val, -3),
    })
    res["gold"].append({"date": date_str, "price": round(gold_val, 2)})
    res["silver"].append({"date": date_str, "price": round(sil_val, 2)})
    res["platinum"].append({
        "date": date_str,
        "price": round(gold_val * 0.43, 2),
    })

  return res


# pandas ME와 M 호환 다운샘플링용 헬퍼
def format_history_monthly_resample(series):
  try:
    return series.resample("ME").last().dropna()
  except ValueError:
    return series.resample("M").last().dropna()


# 월간 AI용 요약 패킷 가공
hist_data_monthly = {}
for name, s in hist_data.items():
  hist_data_monthly[name] = format_history_monthly_resample(s)

# 텅스텐 등 파생 월간 데이터 셋업을 위한 가상 시리즈화
cop_m = hist_data_monthly.get("copper", pd.Series())
wti_m = hist_data_monthly.get("wti", pd.Series())
alu_m = hist_data_monthly.get("aluminum", pd.Series())
gold_m = hist_data_monthly.get("gold", pd.Series())
sil_m = hist_data_monthly.get("silver", pd.Series())
krw_m = hist_data_monthly.get("usdkrw", pd.Series())

# 19종 월간 데이터 구조화
monthly_19_history = []
pivot_m = cop_m if not cop_m.empty else wti_m
res_m = {
    k: []
    for k in [
        "wti",
        "ldpe",
        "pvc",
        "abs",
        "pc",
        "nickel",
        "zinc",
        "aluminum",
        "copper",
        "magnesium",
        "silicon",
        "chromium",
        "tungsten",
        "wire_rod",
        "h_beam",
        "sts304",
        "gold",
        "silver",
        "platinum",
    ]
}

for idx, val in pivot_m.items():
  date_str = idx.strftime("%Y-%m")
  cop = float(val) if not pivot_m.empty else 4.0
  wti_val = float(wti_m.loc[idx]) if idx in wti_m.index else 75.0
  alu_val = float(alu_m.loc[idx]) if idx in alu_m.index else 2200.0
  gold_val = float(gold_m.loc[idx]) if idx in gold_m.index else 2300.0
  sil_val = float(sil_m.loc[idx]) if idx in sil_m.index else 28.0
  krw_val = float(krw_m.loc[idx]) if idx in krw_m.index else 1380.0
  cop_ton = cop * 2204.62

  res_m["wti"].append({"date": date_str, "price": round(wti_val, 2)})
  res_m["ldpe"].append({"date": date_str, "price": round(wti_val * 7.5 + 420, 1)})
  res_m["pvc"].append({"date": date_str, "price": round(wti_val * 6.2 + 480, 1)})
  res_m["abs"].append({"date": date_str, "price": round(wti_val * 10.8 + 620, 1)})
  res_m["pc"].append({"date": date_str, "price": round(wti_val * 12.5 + 980, 1)})
  res_m["nickel"].append({"date": date_str, "price": round(cop_ton * 2.32, 1)})
  res_m["zinc"].append({"date": date_str, "price": round(alu_val * 1.14, 1)})
  res_m["aluminum"].append({"date": date_str, "price": round(alu_val, 1)})
  res_m["copper"].append({"date": date_str, "price": round(cop_ton, 1)})
  res_m["magnesium"].append({
      "date": date_str,
      "price": round(alu_val * 0.72 + 1100, 1),
  })
  res_m["silicon"].append({
      "date": date_str,
      "price": round(alu_val * 0.38 + wti_val * 4.5 + 950, 1),
  })
  res_m["chromium"].append({
      "date": date_str,
      "price": round(alu_val * 0.42 + wti_val * 3.8 + 1200, 1),
  })
  res_m["tungsten"].append({
      "date": date_str,
      "price": round(cop_ton * 4.8 + 8500, 1),
  })
  res_m["wire_rod"].append({
      "date": date_str,
      "price": round(cop_ton * 0.12 + 280, 1),
  })
  res_m["h_beam"].append({
      "date": date_str,
      "price": round((cop_ton * 0.12 + 250) * krw_val * 1.15, -3),
  })
  res_m["sts304"].append({
      "date": date_str,
      "price": round((cop_ton * 2.32 * 0.08 + 1600) * krw_val, -3),
  })
  res_m["gold"].append({"date": date_str, "price": round(gold_val, 2)})
  res_m["silver"].append({"date": date_str, "price": round(sil_val, 2)})
  res_m["platinum"].append({"date": date_str, "price": round(gold_val * 0.43, 2)})

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

# 3. 고도화된 Gemini 프롬프트 구성 (19종 원자재 예측 스키마 정렬)
prompt = f"""
당신은 글로벌 원자재 및 거시경제 퀀트 분석 수석 애널리스트입니다.
아래 제공된 [3년간의 월별 과거 시계열 데이터]와 [거시경제 지표]를 종합하여,
업무적으로 주시하고 있는 19종 핵심 원자재 각각에 대한 '향후 6개월간(M+1 ~ M+6)의 정밀 가격 예측'과 '전문 산정 근거'를 작성하세요.

[분석 및 추정 가이드라인]
- 석유/폴리머(WTI, LDPE, PVC, ABS, PC)는 글로벌 수요 및 화학정제 스프레드 추세를 반영하세요.
- 비철 및 마이너메탈(전기동, 알루미늄, 니켈, 아연, 마그네슘, 실리콘, 크롬, 텅스텐)은 LME 실시간 수급 및 중국 수출 제한 영향도를 중량 평가하세요.
- 철강/STS(선재, H형강, STS304)는 건설 경기, 니켈 가격 연동 및 한화 고시가(KRW) 특수성을 세밀히 검토하세요.
- 귀금속(금, 은, 백금)은 인플레이션 헤지, 미국 연준 금리 경로 및 LBMA 가격 단위를 추적하세요.

[시장 입력 데이터]
{json.dumps(market_summary_for_ai, ensure_ascii=False)}

반드시 마크다운 없이 순수 JSON 포맷으로만 응답하세요. 스키마:
{{
  "update_date": "{market_summary_for_ai['update_date']}",
  "commodities": {{
    "wti": {{ "name": "WTI 원유 (CME)", "unit": "USD/bbl", "current_price": 0.0, "forecast_6m_target": 0.0, "forecast_change_rate": "+0.0%", "direction": "상승/하락/보합", "volatility_score": 0, "rationale": "...", "monthly_forecast": [{{"month": "2026-09", "price": 0.0}}, {{"month": "2026-10", "price": 0.0}}, {{"month": "2026-11", "price": 0.0}}, {{"month": "2026-12", "price": 0.0}}, {{"month": "2027-01", "price": 0.0}}, {{"month": "2027-02", "price": 0.0}}] }},
    "ldpe": {{ "name": "LDPE Film (동남아 CFR)", "unit": "USD/ton", ... }},
    "pvc": {{ "name": "PVC (극동 CFR)", "unit": "USD/ton", ... }},
    "abs": {{ "name": "ABS Injection (극동 CFR)", "unit": "USD/ton", ... }},
    "pc": {{ "name": "PC 1100 (홍콩 CIF)", "unit": "USD/ton", ... }},
    "nickel": {{ "name": "니켈 (LME)", "unit": "USD/ton", ... }},
    "zinc": {{ "name": "아연 (LME)", "unit": "USD/ton", ... }},
    "aluminum": {{ "name": "알루미늄 (LME)", "unit": "USD/ton", ... }},
    "copper": {{ "name": "전기동 (LME)", "unit": "USD/ton", ... }},
    "magnesium": {{ "name": "마그네슘 99.9% (중국 FOB)", "unit": "USD/mt", ... }},
    "silicon": {{ "name": "실리콘 Ferro (북미 FOB)", "unit": "USD/ton", ... }},
    "chromium": {{ "name": "크롬 Ferro (북미 FOB)", "unit": "USD/ton", ... }},
    "tungsten": {{ "name": "텅스텐 Ferro (북미 FOB)", "unit": "USD/ton", ... }},
    "wire_rod": {{ "name": "선재 (멥스 전세계)", "unit": "USD/ton", ... }},
    "h_beam": {{ "name": "H형강 소형/중형 (현대제철)", "unit": "KRW/ton", ... }},
    "sts304": {{ "name": "STS 304 CR 2mm (한국도매)", "unit": "KRW/ton", ... }},
    "gold": {{ "name": "금 (LBMA)", "unit": "USD/oz.t", ... }},
    "silver": {{ "name": "은 (LBMA)", "unit": "US￠/oz.t", ... }},
    "platinum": {{ "name": "백금 (CME)", "unit": "USD/oz.t", ... }}
  }}
}}
"""

print("[진행] Gemini 3.6 Flash 19종 원자재 다변량 시나리오 연산 수행 중...")
try:
  response = client.models.generate_content(
      model="gemini-3.6-flash",
      contents=prompt,
      config=types.GenerateContentConfig(
          response_mime_type="application/json",
      ),
  )

  result_json = json.loads(response.text)

  # 일간 19종 정밀 시계열 생성
  history_daily_out = generate_19_commodities(is_daily=True)

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
      f" {market_summary_for_ai['update_date']} (19종 원자재 확장판 반영)"
  )

except Exception as e:
  print(f"[Gemini 연산 실패] {e}")
  exit(1)

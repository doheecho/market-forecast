import json
import os
from datetime import datetime
from google import genai
from google.genai import types
import yfinance as yf

# 1. API 키 확인 및 클라이언트 생성
api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
  print("[에러] GEMINI_API_KEY 환경변수가 없습니다.")
  exit(1)

client = genai.Client(api_key=api_key)

# 2. 시장 데이터 수집 (구리, 금, 환율)
try:
  copper = yf.Ticker("HG=F").history(period="3mo")["Close"].dropna()
  gold = yf.Ticker("GC=F").history(period="3mo")["Close"].dropna()
  cny = yf.Ticker("CNY=X").history(period="3mo")["Close"].dropna()

  copper_curr = round(float(copper.iloc[-1]), 2) if len(copper) > 0 else 4.25
  gold_curr = round(float(gold.iloc[-1]), 2) if len(gold) > 0 else 2400.0
  cny_curr = round(float(cny.iloc[-1]), 4) if len(cny) > 0 else 7.23
  copper_history = [
      round(float(x), 2) for x in copper.iloc[-10:].tolist()
  ] if len(copper) > 0 else [4.1, 4.2, 4.25]
except Exception as e:
  print(f"[경고] 지표 수집 예외 발생, 기본값 사용: {e}")
  copper_curr = 4.25
  gold_curr = 2400.0
  cny_curr = 7.23
  copper_history = [4.1, 4.2, 4.25]

market_context = {
    "latest_date": datetime.now().strftime("%Y-%m-%d"),
    "copper_current_usd_lb": copper_curr,
    "copper_history_sample": copper_history,
    "gold_current_usd_oz": gold_curr,
    "cny_current_usd": cny_curr,
}

# 3. 프롬프트 구성
prompt = f"""
당신은 원자재 가격 분석 전문 AI입니다. 아래 공개 시황 데이터를 바탕으로 향후 6개월간 전기동(구리) 가격 및 수급 전망을 분석하세요.

[시장 데이터]
{json.dumps(market_context, ensure_ascii=False)}

반드시 마크다운 코드블록(```json) 없이 순수 JSON 포맷으로만 응답하세요:
{{
  "update_date": "{market_context['latest_date']}",
  "current_price": {market_context['copper_current_usd_lb']},
  "forecast_6m_target": 4.85,
  "forecast_change_rate": "+14.1%",
  "direction": "상승",
  "volatility_score": 45,
  "planning_advisor": "향후 6개월간 공급 타이트로 가격 상승이 예상되므로 조기 물량 확보를 권고합니다.",
  "monthly_forecast": [
    {{"month": "M+1", "price": 4.30}},
    {{"month": "M+2", "price": 4.42}},
    {{"month": "M+3", "price": 4.55}},
    {{"month": "M+4", "price": 4.68}},
    {{"month": "M+5", "price": 4.75}},
    {{"month": "M+6", "price": 4.85}}
  ]
}}
"""

# 4. Gemini 3.6 Flash 호출
try:
  response = client.models.generate_content(
      model="gemini-3.6-flash",
      contents=prompt,
      config=types.GenerateContentConfig(
          response_mime_type="application/json",
      ),
  )

  # 5. 결과 JSON 파일 저장
  with open("copper_forecast.json", "w", encoding="utf-8") as f:
    f.write(response.text)

  print(
      f"[성공] copper_forecast.json 생성 완료: {market_context['latest_date']}"
  )

except Exception as e:
  print(f"[Gemini API 호출 실패] {e}")
  exit(1)

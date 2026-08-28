import json
import os
from datetime import datetime
import google.generativeai as genai
import yfinance as yf

# 1. API 키 설정 (GitHub Actions의 Secret 환경변수에서 로드)
api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
  raise ValueError("GEMINI_API_KEY 환경변수가 설정되지 않았습니다.")

genai.configure(api_key=api_key)
model = genai.GenerativeModel("gemini-1.5-flash")

# 2. 공개 시장 지표 수집 (구리 선물, 금 선물, 위안화 환율)
copper = yf.Ticker("HG=F").history(period="6mo")["Close"].dropna()
gold = yf.Ticker("GC=F").history(period="6mo")["Close"].dropna()
cny = yf.Ticker("CNY=X").history(period="6mo")["Close"].dropna()

market_context = {
    "latest_date": datetime.now().strftime("%Y-%m-%d"),
    "copper_current": round(float(copper.iloc[-1]), 2),
    "copper_history_sample": [
        round(float(x), 2) for x in copper.iloc[-30:].tolist()
    ],
    "gold_current": round(float(gold.iloc[-1]), 2),
    "cny_current": round(float(cny.iloc[-1]), 4),
}

# 3. Gemini 예측 프롬프트 구성 (JSON 전용 출력)
prompt = f"""
당신은 원자재 가격 분석 전문 AI입니다. 아래 공개 시황 데이터를 바탕으로 향후 6개월간 전기동(구리) 가격 및 수급 전망을 분석하세요.

[시장 데이터]
{json.dumps(market_context, ensure_ascii=False)}

반드시 마크다운 없이 순수 JSON 포맷으로만 응답하세요:
{{
  "update_date": "{market_context['latest_date']}",
  "current_price": {market_context['copper_current']},
  "forecast_6m_target": 0.0,
  "forecast_change_rate": "+0.0%",
  "direction": "상승",
  "volatility_score": 50,
  "planning_advisor": "향후 6개월 구매 전략 권고사항 2줄 요약",
  "monthly_forecast": [
    {{"month": "M+1", "price": 0.0}},
    {{"month": "M+2", "price": 0.0}},
    {{"month": "M+3", "price": 0.0}},
    {{"month": "M+4", "price": 0.0}},
    {{"month": "M+5", "price": 0.0}},
    {{"month": "M+6", "price": 0.0}}
  ]
}}
"""

response = model.generate_content(
    prompt, generation_config={"response_mime_type": "application/json"}
)

# 4. 결과 JSON 파일로 저장
with open("copper_forecast.json", "w", encoding="utf-8") as f:
  f.write(response.text)

print(f"[성공] copper_forecast.json 파일 생성 완료: {market_context['latest_date']}")

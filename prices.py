"""시세만 갱신 (Gemini 호출 없음). 매일 실행.

raw_materials_forecast.json 의 history_3y·macro·prices_date 만 새로 쓰고,
forecast_data(AI 전망)는 건드리지 않는다. GEMINI_API_KEY 불필요.
"""
from __future__ import annotations

import json
import sys

from _common import build_history, fetch_raw, latest_macro, today_str

raw = fetch_raw()
history, _spot = build_history(raw)
if not any(history.values()):
    sys.exit("[에러] 시세를 하나도 수집하지 못했습니다. 기존 파일 유지.")

try:
    doc = json.load(open("raw_materials_forecast.json", encoding="utf-8"))
except Exception:  # noqa: BLE001
    doc = {}

doc["history_3y"] = history
doc["macro"] = latest_macro(raw)
doc["prices_date"] = today_str()
doc.setdefault("update_date", doc["prices_date"])

with open("raw_materials_forecast.json", "w", encoding="utf-8") as f:
    json.dump(doc, f, ensure_ascii=False, indent=2)
print(f"[성공] 시세 갱신 완료 ({doc['prices_date']})")

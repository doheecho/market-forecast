"""KOMIS(한국자원정보서비스, data.go.kr) 광물가격 수집 — 니켈·아연·텅스텐.

야후 파이낸스에 자유 시세가 없는 품목을 KOMIS Open API 로 받아
raw_materials_forecast.json 의 history_3y 에 이어붙인다.

■ 활성화 방법 (최초 1회)
  1. https://www.data.go.kr 회원가입 후 "한국자원정보서비스 광물가격정보"
     (또는 "국제 광물 가격 정보") 오픈API 활용신청 → 승인
  2. 발급받은 "일반 인증키(Decoding)" 를 GitHub Secret  KOMIS_API_KEY  에 저장
     (로컬 테스트 시 환경변수  set KOMIS_API_KEY=...)
  3. 아래 ENDPOINT / ITEM_CODES / 파서를 승인 문서(요청/응답 명세)에 맞게 확정
     — data.go.kr 상세페이지의 "요청 변수" / "출력 결과" 표를 그대로 반영하면 됨.
     현재 값은 KOMIS 문서를 기준으로 한 '추정치'이므로 실제 명세로 교체 필요.

키가 없거나 호출이 실패하면 아무것도 하지 않고 정상 종료한다(파이프라인 비차단).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

import requests

API_KEY = os.environ.get("KOMIS_API_KEY", "").strip()
JSON_PATH = "raw_materials_forecast.json"

# ── 사용자 확정 필요 구간 ────────────────────────────────────────────────
# data.go.kr "한국자원정보서비스" 상세페이지의 엔드포인트 URL 로 교체.
ENDPOINT = os.environ.get(
    "KOMIS_ENDPOINT",
    "https://apis.data.go.kr/1400377/mineralPriceService/getMineralPriceList",
)
# _common.META 의 키  →  KOMIS 품목코드(또는 품목명 파라미터).
# 승인 문서의 코드표로 교체. (예시값)
ITEM_CODES = {
    "nickel": "NI",
    "zinc": "ZN",
    "tungsten": "W",
}
# 응답에서 날짜/가격 필드명 (승인 문서의 출력결과 표 기준으로 교체)
FIELD_DATE = "priceDate"     # 예: "baseDt", "date"
FIELD_PRICE = "price"        # 예: "prc", "closePrice"
FIELD_ITEM = "itemCd"        # 응답 행이 어떤 품목인지 구분하는 필드
# 요청 파라미터명
PARAM_KEY = "serviceKey"
PARAM_ITEM = "itemCd"
PARAM_START = "beginDt"
PARAM_END = "endDt"
PARAM_ROWS = "numOfRows"
# ────────────────────────────────────────────────────────────────────────

USD_PER_KRW = None  # KOMIS 가 USD 로 주면 None. KRW 로 주면 _common.latest_macro 의 usdkrw 로 나눔.


def _fetch_item(key: str, code: str, start: str, end: str) -> list[dict]:
    params = {
        PARAM_KEY: API_KEY,
        PARAM_ITEM: code,
        PARAM_START: start,
        PARAM_END: end,
        PARAM_ROWS: 4000,
        "_type": "json",
    }
    r = requests.get(ENDPOINT, params=params, timeout=30)
    r.raise_for_status()
    body = r.json()
    # data.go.kr 표준 응답 구조: response.body.items.item  (스키마 확정 시 조정)
    items = (
        body.get("response", {}).get("body", {}).get("items", {}).get("item")
        or body.get("items")
        or body.get("data")
        or []
    )
    if isinstance(items, dict):
        items = [items]
    rows = []
    for it in items:
        d, p = it.get(FIELD_DATE), it.get(FIELD_PRICE)
        if d is None or p is None:
            continue
        ds = str(d)
        if len(ds) == 8 and ds.isdigit():          # 20260828 → 2026-08-28
            ds = f"{ds[:4]}-{ds[4:6]}-{ds[6:]}"
        try:
            price = float(str(p).replace(",", ""))
        except ValueError:
            continue
        rows.append({"date": ds, "price": round(price, 2)})
    rows.sort(key=lambda x: x["date"])
    return rows


def main() -> None:
    if not API_KEY:
        print("[건너뜀] KOMIS_API_KEY 없음 — 니켈·아연·텅스텐 수집 생략.")
        return

    try:
        doc = json.load(open(JSON_PATH, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        doc = {}
    hist = doc.setdefault("history_3y", {})

    end = datetime.now()
    start = end - timedelta(days=365 * 6 + 30)
    s, e = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

    krw = None
    if USD_PER_KRW == "macro":
        krw = (doc.get("macro") or {}).get("usdkrw")

    got = 0
    for key, code in ITEM_CODES.items():
        try:
            rows = _fetch_item(key, code, s, e)
        except Exception as ex:  # noqa: BLE001
            print(f"[경고] KOMIS {key}({code}) 수집 실패: {ex}")
            continue
        if not rows:
            print(f"[경고] KOMIS {key} 응답 0행 — 파서/코드 확인 필요.")
            continue
        if krw:
            for row in rows:
                row["price"] = round(row["price"] / krw, 2)
        hist[key] = rows
        got += 1
        print(f"[성공] KOMIS {key}: {len(rows)}행 ({rows[0]['date']}~{rows[-1]['date']})")

    if not got:
        print("[건너뜀] KOMIS 수집 결과 없음 — 파일 미변경.")
        return

    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print(f"[성공] KOMIS {got}개 품목 병합 완료.")


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:  # noqa: BLE001
        print(f"[경고] komis.py 비정상 종료(무시): {ex}")
        sys.exit(0)

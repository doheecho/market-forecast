"""야후·automated 소스가 없는 원자재(니켈·아연 등) 시세를 수동 파일로 주입.

KOMIS(한국자원정보서비스) 등에서 받은 CSV/엑셀(→CSV 저장)을 읽어
raw_materials_forecast.json 의 history_3y[<key>] 에 병합한다.
이후 prices.py / analyze.py 는 이 키를 야후 갱신 때 덮어쓰지 않고 보존하며,
analyze.py 는 이 시세로 6개월 전망까지 만든다.

사용법:
    python seed_prices.py nickel  nickel_komis.csv
    python seed_prices.py zinc    zinc_komis.csv  --unit-mult 1.0

CSV 요건: 날짜 열 1개 + 가격(종가) 열 1개. 헤더 이름은 자유
(날짜: date/일자/기준일/dt … / 가격: price/close/종가/가격/USD …).
날짜 형식은 YYYY-MM-DD, YYYY.MM.DD, YYYY/MM/DD, YYYYMMDD 모두 허용.
KOMIS 가격이 원화면  --krw  (raw_materials_forecast.json 의 macro.usdkrw 로 USD 환산).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys

JSON_PATH = "raw_materials_forecast.json"
ALLOWED_KEYS = {"nickel", "zinc", "tungsten", "lead", "tin"}  # _common.META 에 있어야 표시됨

_DATE_HINTS = ("date", "일자", "기준", "dt", "ymd", "날짜", "거래일")
_PRICE_HINTS = ("price", "close", "종가", "가격", "usd", "값", "시세", "당월")


def _pick(header: list[str], hints: tuple[str, ...]) -> int:
    for i, h in enumerate(header):
        hl = h.strip().lower()
        if any(k in hl for k in hints):
            return i
    return -1


def _norm_date(s: str) -> str | None:
    s = s.strip().strip('"')
    m = re.match(r"(\d{4})[-./]?(\d{1,2})[-./]?(\d{1,2})", s)
    if not m:
        return None
    y, mo, d = m.groups()
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


def _to_float(s: str) -> float | None:
    s = re.sub(r"[^\d.\-]", "", s.strip())
    try:
        return float(s)
    except ValueError:
        return None


def _read_text(path: str) -> str:
    # KOMIS 엑셀→CSV 는 한국 윈도우에서 cp949/euc-kr 인 경우가 많음.
    for enc in ("utf-8-sig", "cp949", "euc-kr", "latin1"):
        try:
            with open(path, encoding=enc, newline="") as f:
                return f.read()
        except (UnicodeDecodeError, LookupError):
            continue
    sys.exit(f"[에러] {path} 인코딩을 인식하지 못했습니다.")


def load_rows(path: str, unit_mult: float, krw: float | None) -> list[dict]:
    text = _read_text(path)
    head = text[:4096]
    delim = "\t" if "\t" in head.splitlines()[0] else (
        ";" if head.count(";") > head.count(",") else ",")
    rows_in = list(csv.reader(text.splitlines(), delimiter=delim))
    if not rows_in:
        sys.exit("[에러] 빈 파일입니다.")
    header = rows_in[0]
    di = _pick(header, _DATE_HINTS)
    pi = _pick(header, _PRICE_HINTS)
    if di < 0 or pi < 0:
        sys.exit(f"[에러] 날짜/가격 열을 못 찾음. 헤더: {header}\n"
                 f"      날짜 후보 idx={di}, 가격 후보 idx={pi}")
    print(f"[진행] 날짜 열='{header[di]}', 가격 열='{header[pi]}', 구분자={delim!r}")
    seen: dict[str, float] = {}
    for row in rows_in[1:]:
        if len(row) <= max(di, pi):
            continue
        d = _norm_date(row[di])
        v = _to_float(row[pi])
        if not d or v is None or v <= 0:
            continue
        if krw:
            v /= krw
        seen[d] = round(v * unit_mult, 2)
    return [{"date": d, "price": p} for d, p in sorted(seen.items())]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("key", choices=sorted(ALLOWED_KEYS))
    ap.add_argument("csv_path")
    ap.add_argument("--unit-mult", type=float, default=1.0,
                    help="표기 단위 배수 (예: USD/lb→USD/ton 이면 2204.62)")
    ap.add_argument("--krw", action="store_true",
                    help="가격이 원화이면 macro.usdkrw 로 USD 환산")
    ap.add_argument("--replace", action="store_true",
                    help="기존 시세를 합치지 않고 통째로 교체")
    args = ap.parse_args()

    try:
        doc = json.load(open(JSON_PATH, encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        sys.exit(f"[에러] {JSON_PATH} 읽기 실패: {e}")

    krw = None
    if args.krw:
        krw = (doc.get("macro") or {}).get("usdkrw")
        if not krw:
            sys.exit("[에러] --krw 인데 macro.usdkrw 가 없음. analyze/prices 를 먼저 한 번 실행하세요.")
        print(f"[진행] 원화→USD 환산: /{krw}")

    rows = load_rows(args.csv_path, args.unit_mult, krw)
    if len(rows) < 30:
        sys.exit(f"[에러] 유효 데이터가 너무 적음({len(rows)}행). 파일 확인 필요.")

    hist = doc.setdefault("history_3y", {})
    if not args.replace and hist.get(args.key):
        merged = {r["date"]: r["price"] for r in hist[args.key]}
        merged.update({r["date"]: r["price"] for r in rows})
        rows = [{"date": d, "price": p} for d, p in sorted(merged.items())]
    hist[args.key] = rows

    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print(f"[성공] {args.key}: {len(rows)}행 저장 "
          f"({rows[0]['date']}~{rows[-1]['date']}, 최근 {rows[-1]['price']})")
    print("      이제 git add raw_materials_forecast.json && git commit && git push")


if __name__ == "__main__":
    main()

"""analyze.py(주간 AI 전망)와 prices.py(일간 시세 갱신)가 공유하는 수집 로직.

── 이 파이프라인 전체에서 쓰는 통계적 가정 요약 ──────────────────────────
(실제 정의/계산은 각 파일에 있고, 여기는 한눈에 보기 위한 요약)

[이상치 탐지 — 이 파일, _despike_tail]
  · 최근 20영업일(_MAD_WINDOW) 의 median·MAD(중앙값절대편차) 기준
    robust z-score 가 3.5(_MAD_THRESHOLD) 초과인 마지막 값만 이상치로 잘라냄.
  · 고정 %가 아니라 원자재별 실제 변동성을 반영(변동성 큰 니켈은 덜 민감하게,
    변동성 낮은 금은 더 민감하게 반응).

[base(중심 전망) — analyze.py, conservative_forecast / sanitize_scenarios]
  · 통계 중심선: 최근 12개월(_DRIFT_WINDOW) 평균 로그수익률(드리프트)의 20%
    (_DRIFT_DAMPING)만 반영 — 추세를 그대로 미래로 외삽하는 과신 방지.
  · 밴드(bull/bear 끝점): 최근 36개월(_VOL_WINDOW) 로그수익률 표준편차(σ)를
    랜덤워크 √t 스케일링, z=1.28(_BAND_Z, 약 80% 구간)로 t개월 뒤 밴드 계산.
    이 끝점은 AI 가 못 건드리는 순수 통계값.
  · base 의 최종 위치: 위 밴드(bear~bull) 안에서 AI 가 서술한 방향성 비대칭
    (bull/bear bias 상대 비율)만큼 가중 이동. 가중치 _AI_TILT_WEIGHT=0.5로
    확정 — "과거 데이터 기반 통계"와 "뉴스·정책 등 AI 판단"을 절반씩 결합
    (예측 결합, forecast combination). base 는 절대 밴드 밖으로 못 나감.

[유사국면(analogs) — analyze.py, top_analogs]
  · 정량적 예측 근거가 아니라 AI 서술(사건명·요약)을 위한 참고 자료로만 취급
    (표본이 적은 슬라이딩 윈도우 상관은 데이터 스누핑·허위상관 위험이 있음).
─────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import csv
import os
import re
from datetime import datetime

import pandas as pd
import yfinance as yf

TICKERS = {
    "copper": "HG=F", "aluminum": "ALI=F", "wti": "CL=F",
    "gold": "GC=F", "silver": "SI=F", "platinum": "PL=F",
    "steel": "HRC=F", "ironore": "TIO=F",
    "dxy": "DX-Y.NYB", "us10y": "^TNX", "usdcny": "CNY=X", "usdkrw": "KRW=X",
}

# 야후에서 받는 원자재. 야후에 없는 품목(니켈·아연·텅스텐)은 manual/<key>.csv 로 주입.
YF_COMMODITIES = ["wti", "copper", "aluminum", "gold", "silver", "platinum", "steel", "ironore"]
COMMODITIES = list(YF_COMMODITIES)  # 파이프라인이 참조하는 전체 목록 (확장 가능)

MANUAL_DIR = "manual"  # manual/nickel.csv, manual/zinc.csv, manual/tungsten.csv …

# name, unit, 가격 배수(야후 원값 → 표기 단위)
META = {
    "wti": ("WTI 원유 (NYMEX Futures)", "USD/bbl", 1.0),
    "copper": ("전기동 (LME 현물)", "USD/ton", 2204.62),  # HG=F: USD/lb → USD/ton
    "aluminum": ("알루미늄 (LME 현물)", "USD/ton", 1.0),
    "gold": ("금 (LBMA 현물)", "USD/ozt", 1.0),
    "silver": ("은 (LBMA 현물)", "US￠/ozt", 100.0),  # SI=F: USD/oz → US￠/ozt (x100)
    "platinum": ("백금 (LPPM 현물)", "USD/ozt", 1.0),
    "steel": ("열연강판 (CME HRC)", "USD/s.ton", 1.0),  # 냉연 SPCC 아님 — 방향성 참고
    "ironore": ("철광석 (중국 칭다오항 CFR Fines 현물)", "USD/ton", 1.0),
    # 야후에 자유 시세가 없음 — manual/<key>.csv (date,price) 로 주입하면 대시보드에 자동 표시.
    # CSV 가격은 표기 단위 그대로 (배수 적용 안 함).
    "nickel": ("니켈 (LME 현물)", "USD/ton", 1.0),
    "zinc": ("아연 (LME 현물)", "USD/ton", 1.0),
    "tungsten": ("텅스텐 (중국 현물 Oxide WO3 99.95%)", "RMB/mt", 1.0),
    "silicon": ("실리콘 (중국 FOB Ferro 75% 현물)", "USD/ton", 1.0),
}

_DATE_HINTS = ("date", "일자", "기준", "dt", "ymd", "날짜", "거래일", "월")
_PRICE_HINTS = ("price", "close", "종가", "가격", "usd", "값", "시세", "당월", "평균")


def _read_price_csv(path: str) -> list[dict]:
    """date,price 형태의 CSV → [{date:'YYYY-MM-DD', price:float}]. 인코딩/열이름 자동 판별."""
    text = None
    for enc in ("utf-8-sig", "cp949", "euc-kr", "latin1"):
        try:
            with open(path, encoding=enc, newline="") as f:
                text = f.read()
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        print(f"[경고] {path} 인코딩 인식 실패 — 건너뜀")
        return []

    lines = text.splitlines()
    if not lines:
        return []
    head = lines[0]
    delim = "\t" if "\t" in head else (";" if head.count(";") > head.count(",") else ",")
    rows = list(csv.reader(lines, delimiter=delim))
    header = rows[0]

    def pick(hints):
        for i, h in enumerate(header):
            if any(x in h.strip().lower() for x in hints):
                return i
        return -1

    di, pi = pick(_DATE_HINTS), pick(_PRICE_HINTS)
    if di < 0 or pi < 0:
        print(f"[경고] {path}: 날짜/가격 열을 못 찾음 (헤더 {header}) — 건너뜀")
        return []

    seen: dict[str, float] = {}
    for r in rows[1:]:
        if len(r) <= max(di, pi):
            continue
        md = re.match(r"(\d{4})[-./]?(\d{1,2})[-./]?(\d{1,2})?", r[di].strip().strip('"'))
        if not md:
            continue
        y, mo, d = md.group(1), md.group(2), md.group(3) or "01"
        try:
            v = float(re.sub(r"[^\d.\-]", "", r[pi].strip()))
        except ValueError:
            continue
        if v > 0:
            seen[f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"] = round(v, 2)
    return [{"date": k, "price": v} for k, v in sorted(seen.items())]


def load_manual_history() -> dict[str, list[dict]]:
    """manual/<key>.csv 를 모두 읽어 {key: [{date,price}]} 반환. 폴더 없으면 {}."""
    out: dict[str, list[dict]] = {}
    if not os.path.isdir(MANUAL_DIR):
        return out
    for fn in sorted(os.listdir(MANUAL_DIR)):
        if not fn.lower().endswith(".csv"):
            continue
        # 파일명을 정리하여 소문자화 및 공백/하이픈/언더바 제거
        key = os.path.splitext(fn)[0].strip().lower()
        key = key.replace(" ", "").replace("_", "").replace("-", "")
        
        # 대표적인 사용자 입력 오타/별칭 보정
        if key == "silicone":
            key = "silicon"
        elif key == "aluminium":
            key = "aluminum"
            
        if key not in META:
            print(f"[경고] manual/{fn}: META 에 없는 키 '{key}' — 무시")
            continue
        rows = _read_price_csv(os.path.join(MANUAL_DIR, fn))
        if len(rows) >= 20:
            out[key] = rows
            print(f"[진행] 수동 시세 로드: {key} ({len(rows)}행, {rows[0]['date']}~{rows[-1]['date']})")
        elif rows:  # 헤더만 있는 빈 템플릿은 조용히 무시, 데이터가 부족할 때만 경고
            print(f"[경고] manual/{fn}: 유효 {len(rows)}행(<20) — 건너뜀")
    return out


def fetch_raw() -> dict[str, pd.Series]:
    """야후 티커 6년 일봉 종가를 yf.download 한 번으로 병렬 수집."""
    print(f"[진행] 야후 파이낸스 시계열 수집 중… ({len(TICKERS)}개 티커 일괄)")
    dl = yf.download(
        list(TICKERS.values()), period="6y", interval="1d",
        auto_adjust=True, progress=False, threads=True, group_by="column",
    )
    try:
        close = dl["Close"]
    except Exception:  # noqa: BLE001
        close = dl

    raw: dict[str, pd.Series] = {}
    for name, tk in TICKERS.items():
        try:
            s = close[tk].dropna()
            if getattr(s.index, "tz", None) is not None:
                s.index = s.index.tz_localize(None)
            raw[name] = s
        except Exception as e:  # noqa: BLE001
            print(f"[경고] {name}({tk}) 수집 실패: {e}")
            raw[name] = pd.Series(dtype=float)
    return raw


# ── 이상치(outlier) 처리 ────────────────────────────────────────────────
# 기존 "직전 8개 중앙값 대비 ±12%" 같은 고정 퍼센트 규칙은 원자재마다 실제
# 변동성이 다른데도 동일한 문턱을 쓰는 문제가 있었음(니켈처럼 원래 변동성이
# 큰 자산은 정상적인 움직임도 잘려나가고, 금처럼 변동성이 낮은 자산은
# 진짜 이상치를 못 거를 수 있음).
#
# → Robust Z-score(MAD 기반)로 교체: 최근 구간의 "중앙값 절대편차(MAD)"를
#   변동성 척도로 쓰고, 그 자산 고유의 변동성 대비 몇 배(threshold) 벗어났는지로
#   판정. MAD 는 평균/표준편차보다 극단값 자체에 덜 휘둘리는 robust 통계량이라
#   "이상치를 걸러내기 위한 척도"로 표준편차보다 적합함.
#   robust z = 0.6745 * (x - median) / MAD  (0.6745 는 정규분포 가정 시 표준편차와
#   맞추기 위한 보정상수), |robust z| > threshold(기본 3.5) 를 이상치로 판정.
_MAD_WINDOW = 20      # 최근 며칠(영업일) 을 기준으로 median/MAD 계산
_MAD_THRESHOLD = 3.5  # 이 값 초과시 이상치 (3.5는 통계학에서 흔히 쓰는 robust 기준)


def _robust_zscore(values: list[float], x: float) -> float:
    if not values:
        return 0.0
    med = sorted(values)[len(values) // 2]
    abs_dev = sorted(abs(v - med) for v in values)
    mad = abs_dev[len(abs_dev) // 2]
    if mad == 0:
        return 0.0
    return 0.6745 * (x - med) / mad


def _despike_tail(rows: list[dict]) -> list[dict]:
    """꼬리에 튄 값(야후 마지막 봉 오류)을 MAD 기반 robust z-score 로 판정해 잘라냄.

    직전 _MAD_WINDOW 개 값의 median/MAD 대비 robust z-score 가 _MAD_THRESHOLD 를
    넘는 마지막 값들을 순차적으로 제거한다. 고정 퍼센트 대신 그 자산 고유의
    최근 변동성을 기준으로 삼기 때문에, 원래 변동성이 큰 원자재(니켈 등)를
    과도하게 잘라내거나 변동성이 낮은 원자재(금 등)의 이상치를 놓치는 문제를 줄인다.
    """
    end = len(rows)
    while end > _MAD_WINDOW + 1:
        window = [r["price"] for r in rows[end - _MAD_WINDOW - 1:end - 1]]
        z = _robust_zscore(window, rows[end - 1]["price"])
        if abs(z) > _MAD_THRESHOLD:
            end -= 1
        else:
            break
    return rows[:end]


def build_history(raw: dict[str, pd.Series]) -> tuple[dict[str, list[dict]], dict[str, float]]:
    """원자재 6종 → 공통 타임라인 정렬 + 배수 적용 + 꼬리 despike. (history, spot) 반환."""
    master = pd.Series(dtype=float)
    for k in COMMODITIES:
        if len(raw.get(k, [])) > len(master):
            master = raw[k]
    timeline = master.index

    history: dict[str, list[dict]] = {}
    spot: dict[str, float] = {}
    for k in COMMODITIES:
        mult = META[k][2]
        s = raw.get(k, pd.Series(dtype=float)).reindex(timeline).ffill().bfill()
        rows = [
            {"date": ts.strftime("%Y-%m-%d"), "price": round(float(v) * mult, 2)}
            for ts, v in s.items()
            if pd.notna(v)
        ]
        rows = _despike_tail(rows)
        history[k] = rows
        if rows:
            spot[k] = rows[-1]["price"]
    return history, spot


def latest_macro(raw: dict[str, pd.Series]) -> dict[str, float]:
    def last(name: str, default: float) -> float:
        s = raw.get(name)
        return round(float(s.iloc[-1]), 4) if s is not None and not s.empty else default

    return {
        "dxy": last("dxy", 104.2),
        "us10y": last("us10y", 4.15),
        "usdcny": last("usdcny", 7.23),
        "usdkrw": last("usdkrw", 1380.0),
    }


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")

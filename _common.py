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
  · 밴드(bull/bear 끝점): 변동성 term-structure = GARCH(1,1) h개월 누적
    (단계 F, garch_sigma_path — √t 대체). 분위계수는 정규 ±1.2816 이 기본이나
    calibration.json(단계 C, split-conformal 실측 잔차분위)이 있으면 horizon별
    (z_lo, z_hi)로 대체 — 팩테일·상하방 비대칭. 근거는 backtest.py 참고.
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
import glob
import json
import os
import re
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

TICKERS = {
    "copper": "HG=F", "aluminum": "ALI=F", "wti": "CL=F",
    "gold": "GC=F", "silver": "SI=F", "platinum": "PL=F",
    "steel": "HRC=F", "ironore": "TIO=F",
    "dxy": "DX-Y.NYB", "us10y": "^TNX", "usdcny": "CNY=X", "usdkrw": "KRW=X",
}

# 시세 소스 우선순위: manual/<key>.csv (1차) → 야후 폴백.
# 아래는 "야후 티커가 있는" 품목일 뿐, CSV 가 있으면(merge_manual) CSV 가 이긴다.
# 현재 실제로 야후 폴백을 타는 건 CSV 가 없는 steel 뿐.
YF_COMMODITIES = ["wti", "copper", "aluminum", "gold", "silver", "platinum", "steel", "ironore"]
COMMODITIES = list(YF_COMMODITIES)  # 파이프라인이 참조하는 전체 목록 (CSV-only 품목은 실행 중 편입)

MANUAL_DIR = "manual"  # 1차 시세 소스: manual/<key>.csv (date,price)

# name, unit, 가격 배수(야후 원값 → 표기 단위). CSV 로 들어온 값에는 배수를 적용하지 않는다.
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
    "hbeam" : ("H형강 소형/중형 (한국 1차 유통가)", "KRW/ton", 1.0),
    "crc" : ("냉연코일 (MEPS 현물)", "USD/ton", 1.0),
    "scrapheavya" : ("고철 중량A (한국 도매가)", "KRW/ton", 1.0),
    "scrapprime" : ("고철 생철 (한국 도매가)", "KRW/ton", 1.0),
    "wirerod" : ("선재 (MEPS 현물)", "USD/ton", 1.0),
    "sts304" : ("STS 304 CR 2mm (한국 도매가)", "KRW/ton", 1.0),
    "hrc" : ("열연코일 (MEPS 현물)", "USD/ton", 1.0),
}

_DATE_HINTS = ("date", "일자", "기준", "dt", "ymd", "날짜", "거래일", "월")
_PRICE_HINTS = ("price", "close", "종가", "가격", "usd", "값", "시세", "당월", "평균")


def _read_price_csv(path: str) -> list[dict]:
    """date,price 형태 of CSV → [{date:'YYYY-MM-DD', price:float}]. 인코딩/열이름 자동 판별."""
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


def merge_manual(
    history: dict[str, list[dict]],
    spot: dict[str, float] | None = None,
    raw: dict[str, "pd.Series"] | None = None,
) -> dict[str, list[dict]]:
    """manual/<key>.csv 를 1차 시세 소스로 삼아 history 에 덮어쓴다.

    · CSV(유효 20행+)가 있는 키는 그 값으로 history[key] 를 교체하고,
      raw(야후) 시리즈까지 비워 통계 밴드·유사국면·EWMA 변동성 계산도 모두
      CSV 기준이 되게 한다(대시보드 표시가와 전망 근거의 단위/출처 불일치 방지).
    · CSV 가 없거나 부족한 키(steel 등)는 손대지 않아 야후 값이 폴백으로 남는다.

    반환: 이번에 실제로 적용된 {key: rows} (호출측이 COMMODITIES 편입 등에 사용).
    """
    manual = load_manual_history()
    for k, rows in manual.items():
        history[k] = rows
        if spot is not None:
            spot[k] = rows[-1]["price"]
        if raw is not None:
            raw[k] = pd.Series(dtype=float)  # 야후 경로 무력화 → 이하 전 계산이 CSV 기준
    if manual:
        print(f"[진행] CSV 1차 소스 적용: {', '.join(sorted(manual))} "
              f"(나머지는 야후 폴백)")
    return manual


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


# ── 단계 C: 통계 밴드 캘리브레이션 계수 ─────────────────────────────────
# backtest.py --emit-calibration 이 만든 calibration.json 을 읽어, 통계 밴드의
# 분위계수를 정규 ±1.2816(80%) 대신 "표준화 잔차의 실측 분위" 로 바꾼다.
# 파일이 없으면 {} → 호출측이 정규계수로 항등 처리(=기존 동작).
CALIBRATION_FILE = "calibration.json"
PHI_LO, PHI_HI = -1.2816, 1.2816   # Φ⁻¹(0.10), Φ⁻¹(0.90)


def garch_sigma_path(lr, max_h: int, floor: float):
    """단계 F: 월간 로그수익률 → [σ_1..σ_max_h] (h개월 누적 로그표준편차).

    √h(월간분산 일정 가정) 대신 GARCH(1,1) σ²_t = ω + α·ε²_{t-1} + β·σ²_{t-1}.
    분산타게팅으로 ω=v̄(1-α-β) 고정, (α,β)는 조립그리드에서 가우시안 LL 최대화
    (scipy 불필요). h개월 누적분산 = h·v̄ + (σ²_{t+1}-v̄)·(1-(α+β)^h)/(1-(α+β)).
    데이터<24개월이면 √h 로 폴백. **backtest.py _garch_sigma_path 와 동일 구현 —
    한쪽 고치면 양쪽.**
    """
    import numpy as _np

    lr = _np.asarray(lr, dtype=float)
    lr = lr[_np.isfinite(lr)]
    n = len(lr)
    v_bar = float(_np.var(lr, ddof=1)) if n > 1 else 0.0
    fb = _np.array([max(v_bar ** 0.5, floor) * (h ** 0.5) for h in range(1, max_h + 1)])
    if n < 24 or v_bar <= 0:
        return fb
    r = lr - float(lr.mean())
    r2 = r * r
    grid_a = [round(0.02 + 0.03 * i, 2) for i in range(10)]      # 0.02..0.29
    grid_b = [round(0.60 + 0.03 * i, 2) for i in range(12)]      # 0.60..0.93
    best = None
    for al in grid_a:
        for be in grid_b:
            if al + be >= 0.999:
                continue
            om = v_bar * (1.0 - al - be)
            s2 = v_bar
            ll = 0.0
            ok = True
            for t in range(1, n):
                s2 = om + al * r2[t - 1] + be * s2
                if s2 <= 1e-12:
                    ok = False
                    break
                ll -= 0.5 * (float(_np.log(s2)) + r2[t] / s2)
            if ok and (best is None or ll > best[0]):
                best = (ll, al, be, s2)
    if best is None:
        return fb
    _, al, be, s2_last = best
    ab = al + be
    s2_next = v_bar * (1.0 - ab) + al * r2[-1] + be * s2_last
    out = []
    for h in range(1, max_h + 1):
        geo = h if abs(1.0 - ab) < 1e-9 else (1.0 - ab ** h) / (1.0 - ab)
        cum = h * v_bar + (s2_next - v_bar) * geo
        out.append(max(cum, (floor ** 2) * h) ** 0.5)
    return _np.array(out)


def load_calibration(path: str = CALIBRATION_FILE) -> dict[int, tuple[float, float]]:
    """→ {horizon(int): (z_lo, z_hi)}  (bear·bull 밴드용 하방/상방 계수).
    중앙값은 건드리지 않는다(qz_band['0.5']=0). 백테스트상 중앙값까지 옮기면
    국면 편향을 좇아 MAE·pinball 이 악화됐기 때문(band-only 만 채택)."""
    try:
        d = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[int, tuple[float, float]] = {}
    for hs, v in (d.get("by_horizon") or {}).items():
        qb = (v or {}).get("qz_band") or {}
        try:
            lo, hi = float(qb["0.1"]), float(qb["0.9"])
        except (KeyError, TypeError, ValueError):
            continue
        if lo < 0 < hi:                    # 방향성 sanity
            out[int(hs)] = (lo, hi)
    return out


# ── 전망 이력 & 정확도 (과거 예측 vs 실제) ─────────────────────────────
# analyze.py 가 매 배치(주 1회 월요일)마다 그날 전망을 '동결' 보관하고,
# 이미 지나간 예측월은 실제가와 대조해 오차를 누적 기록한다.
# 목적: 과거 전망의 정확도(품목별·기간별 MAE·편향·밴드적중률, 나이브 대비)를
#       계량화해서 앞으로의 전망에 어떻게 반영할지 판단.
SNAP_DIR = "snapshots/forecast"         # 배치 1회 = 파일 1개 (snapshots/forecast/YYYY-MM-DD.json)
SNAP_INDEX = "snapshots/index.json"     # 전 스냅샷의 6개월 타겟·현재가·변화율 요약
SNAP_ACCURACY = "snapshots/accuracy.json"  # 지나간 예측월 × 실제가 대조 원장 + 집계

_KST = timezone(timedelta(hours=9))     # 배치는 월요일 07:00 KST — 이력은 KST 로 기록

# 스냅샷에 담을 스칼라 필드 (전망 '수치'만 — advisor/metrics/analogs 서술은 제외해 가볍게)
_SNAP_FIELDS = (
    "name", "unit", "current_price", "forecast_6m_target",
    "forecast_change_rate", "volatility_score",
)
_SNAP_ARRAYS = ("monthly_forecast_base", "monthly_forecast_bull", "monthly_forecast_bear")


def _now_kst() -> str:
    return datetime.now(_KST).strftime("%Y-%m-%d %H:%M")


def _monthly_actuals(history: dict) -> dict:
    """history_3y({key:[{date,price}]}) → {key: {'YYYY-MM': 그 달 마지막 실제가}}."""
    out: dict[str, dict[str, float]] = {}
    for k, rows in (history or {}).items():
        mp: dict[str, float] = {}
        for r in rows or []:
            d, p = str(r.get("date") or ""), r.get("price")
            if len(d) >= 7 and p:
                mp[d[:7]] = float(p)   # 같은 달이면 뒤 날짜가 덮어씀 = 월말값
        if mp:
            out[k] = mp
    return out


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def _build_accuracy(history: dict, cur_ym: str) -> dict:
    """모든 스냅샷 × 이미 끝난 예측월을 실제가와 대조해 원장+집계를 만든다."""
    actuals = _monthly_actuals(history)
    records = []
    for p in sorted(glob.glob(f"{SNAP_DIR}/*.json")):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        fdate = d.get("update_date") or os.path.splitext(os.path.basename(p))[0]
        fym = fdate[:7]
        for k, c in (d.get("commodities") or {}).items():
            amap = actuals.get(k) or {}
            base = {r.get("month"): r.get("price") for r in c.get("monthly_forecast_base") or []}
            bull = {r.get("month"): r.get("price") for r in c.get("monthly_forecast_bull") or []}
            bear = {r.get("month"): r.get("price") for r in c.get("monthly_forecast_bear") or []}
            anchor = c.get("current_price")
            for ym, pb in base.items():
                if not ym or ym >= cur_ym:      # 아직 안 끝난 달은 제외
                    continue
                act = amap.get(ym)
                if not act or not pb:
                    continue
                lo, hi = bear.get(ym), bull.get(ym)
                err = (act / pb - 1) * 100
                rec = {
                    "forecast_date": fdate,
                    "target_month": ym,
                    "commodity": k,
                    "months_ahead": (int(ym[:4]) * 12 + int(ym[5:7]))
                                    - (int(fym[:4]) * 12 + int(fym[5:7])),
                    "predicted_base": round(float(pb), 2),
                    "predicted_bull": round(float(hi), 2) if hi else None,
                    "predicted_bear": round(float(lo), 2) if lo else None,
                    "anchor_price": round(float(anchor), 2) if anchor else None,
                    "actual": round(float(act), 2),
                    "error_pct": round(err, 2),
                    "abs_error_pct": round(abs(err), 2),
                    "in_band": (lo is not None and hi is not None and lo <= act <= hi),
                    "naive_abs_error_pct": (round(abs(act / anchor - 1) * 100, 2)
                                            if anchor else None),
                }
                records.append(rec)

    def agg(rows):
        if not rows:
            return {"n": 0}
        return {
            "n": len(rows),
            "mae_pct": _mean([r["abs_error_pct"] for r in rows]),
            "bias_pct": _mean([r["error_pct"] for r in rows]),      # +면 실제가 전망보다 높았음(과소전망)
            "band_hit_rate": round(sum(r["in_band"] for r in rows) / len(rows), 3),
            "naive_mae_pct": _mean([r["naive_abs_error_pct"] for r in rows]),
        }

    by_c, by_h = {}, {}
    for r in records:
        by_c.setdefault(r["commodity"], []).append(r)
        by_h.setdefault(str(r["months_ahead"]), []).append(r)
    return {
        "updated_at": _now_kst(),
        "note": "error_pct = (실제/전망_base - 1)*100. bias_pct>0 = 실제가 전망보다 높았음(전망이 과소). "
                "naive = '현재가 그대로 유지' 가정 오차. model MAE < naive MAE 여야 전망이 값어치 있음.",
        "overall": agg(records),
        "by_commodity": {k: agg(v) for k, v in sorted(by_c.items())},
        "by_horizon": {k: agg(v) for k, v in sorted(by_h.items(), key=lambda x: int(x[0]))},
        "records": records,
    }


def save_snapshot(update_date: str, macro: dict, commodities: dict,
                  history: dict | None = None) -> str:
    """이번 배치 전망을 snapshots/forecast/<YYYY-MM-DD>.json 로 동결 보관하고
    snapshots/index.json 을 재생성한다. history 를 주면 snapshots/accuracy.json
    (지나간 예측월 vs 실제가 원장·집계)도 다시 만든다. 같은 날 재실행 시 덮어씀.
    시각은 KST 'YYYY-MM-DD HH:MM'. 저장 경로를 반환.
    """
    os.makedirs(SNAP_DIR, exist_ok=True)
    now = datetime.now(_KST)
    stamp = now.strftime("%Y-%m-%d %H:%M")
    snap_date = now.strftime("%Y-%m-%d")     # 파일명은 KST 날짜 = 사용자가 아는 '그 월요일'

    snap_c = {}
    for k, c in commodities.items():
        row = {f: c.get(f) for f in _SNAP_FIELDS if f in c}
        for arr in _SNAP_ARRAYS:
            row[arr] = [
                {"month": r.get("month"), "price": r.get("price")}
                for r in (c.get(arr) or [])
            ]
        snap_c[k] = row
    snap = {
        "captured_at": stamp,           # KST YYYY-MM-DD HH:MM
        "update_date": update_date,     # analyze.py 기준일(UTC) — 교차확인용
        "macro": macro,
        "commodities": snap_c,
    }
    path = f"{SNAP_DIR}/{snap_date}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)

    # 인덱스 재생성: 모든 스냅샷 파일을 날짜순으로 스캔해 요약만 추림
    entries = []
    for p in sorted(glob.glob(f"{SNAP_DIR}/*.json")):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cs = d.get("commodities", {})
        entries.append({
            "date": os.path.splitext(os.path.basename(p))[0],
            "captured_at": d.get("captured_at"),
            "file": os.path.relpath(p, "snapshots").replace(os.sep, "/"),
            "target_6m": {k: v.get("forecast_6m_target") for k, v in cs.items()},
            "current": {k: v.get("current_price") for k, v in cs.items()},
            "change_rate": {k: v.get("forecast_change_rate") for k, v in cs.items()},
        })
    with open(SNAP_INDEX, "w", encoding="utf-8") as f:
        json.dump({"updated_at": stamp, "snapshots": entries}, f,
                  ensure_ascii=False, indent=2)
    print(f"[성공] 전망 스냅샷 보관: {path} (이력 {len(entries)}개)")

    # 정확도 원장 (지나간 예측월이 있어야 레코드가 생김 — 초기엔 빈 배열)
    if history is not None:
        acc = _build_accuracy(history, now.strftime("%Y-%m"))
        with open(SNAP_ACCURACY, "w", encoding="utf-8") as f:
            json.dump(acc, f, ensure_ascii=False, indent=2)
        ov = acc["overall"]
        if ov.get("n"):
            print(f"[성공] 정확도 원장: {ov['n']}건 · MAE {ov['mae_pct']}% "
                  f"(나이브 {ov['naive_mae_pct']}%) · 밴드적중 {ov['band_hit_rate']}")
        else:
            print("[진행] 정확도 원장: 아직 끝난 예측월 없음(빈 원장)")
    return path

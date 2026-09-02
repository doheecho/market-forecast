"""backtest.py — 통계 전망의 과거 스킬 측정 (워크포워드 / walk-forward).

live 파이프라인(analyze.py)을 전혀 건드리지 않는 독립 스크립트다.
`raw_materials_forecast.json` 의 `history_3y`(월말로 리샘플)만 입력으로,
각 과거 시점 t 에서 **t 까지의 데이터로만** h=1..6 개월 전망을 만들어
그 뒤 실제로 나온 값과 대조한다.

전망 방법론은 analyze.py 의 `calculate_statistical_bounds` 와 동일한
"기하 랜덤워크 + 감쇠 드리프트 + √h 변동성 스케일" 을 재현하되, 핵심
파라미터를 CLI 로 바꿀 수 있게 해 이후 단계(B~F)에서 튜닝 근거로 쓴다.
(Gemini 블렌드는 과거를 값싸게 재생할 수 없어 제외 — 여기서 재는 것은
"통계 모델 단독" 의 스킬, 즉 전체 전망 품질의 하한선이다.)

채점: pinball loss(분위손실, proper scoring rule) · 중앙값 MAE% · 편향% ·
밴드 커버리지(80%/50%) · PIT(확률적분변환) 균일성 · 나이브/드리프트나이브/
계절나이브 대비 스킬 · Diebold-Mariano 검정(HLN 소표본 보정).

실행:  python backtest.py            # 기본 파라미터
       python backtest.py --drift-damping 0.3 --vol-long 24 --vol-blend 0.7
출력:  backtest/results.json (집계·설정·검정)  ·  backtest/records.csv (건별)

── 파라미터의 의미/근거는 이 파일 맨 아래 "가정·근거" 블록에 상세히 적었다. ──
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
from datetime import datetime, timedelta, timezone
from statistics import NormalDist

import numpy as np
import pandas as pd

_N01 = NormalDist()          # 표준정규
_KST = timezone(timedelta(hours=9))
OUT_DIR = "backtest"
DATA_FILE = "raw_materials_forecast.json"

# 채점에 쓰는 분위 레벨. 0.10/0.90 = 80% 밴드(analyze.py z=1.28 과 동일), 0.25/0.75 = 50% 밴드.
QUANTILES = (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)


# ─────────────────────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────────────────────
class Cfg:
    def __init__(self, a: argparse.Namespace):
        self.drift_damping = a.drift_damping   # 드리프트(추세)를 이만큼만 반영
        self.drift_win = a.drift_win           # 드리프트용 최근 개월수
        self.vol_long = a.vol_long             # 장기 변동성 창(개월)
        self.vol_short = a.vol_short           # 단기 변동성 창(개월)
        self.vol_blend = a.vol_blend           # 변동성 = blend*장기 + (1-blend)*단기
        self.vol_floor = a.vol_floor           # 월간 로그수익률 표준편차 하한
        self.min_train = a.min_train           # 이 개월수 이상 쌓여야 전망 시작
        self.horizons = tuple(range(1, a.max_h + 1))
        self.max_h = a.max_h

    def as_dict(self) -> dict:
        return {
            "drift_damping": self.drift_damping, "drift_win": self.drift_win,
            "vol_long": self.vol_long, "vol_short": self.vol_short,
            "vol_blend": self.vol_blend, "vol_floor": self.vol_floor,
            "min_train": self.min_train, "max_h": self.max_h,
            "quantiles": list(QUANTILES),
        }


# ─────────────────────────────────────────────────────────────────────────
# 데이터
# ─────────────────────────────────────────────────────────────────────────
def load_monthly(data_file: str) -> dict[str, pd.Series]:
    """history_3y({key:[{date,price}]}) → {key: 월말 종가 시리즈(pd.Series, DatetimeIndex)}."""
    doc = json.load(open(data_file, encoding="utf-8"))
    hist = doc.get("history_3y") or {}
    out: dict[str, pd.Series] = {}
    for k, rows in hist.items():
        if not rows:
            continue
        s = pd.Series(
            {pd.Timestamp(r["date"]): float(r["price"])
             for r in rows if r.get("price") and r.get("date")}
        ).sort_index()
        m = s.resample("ME").last().dropna()
        if len(m) >= 24:
            out[k] = m
    return out


# ─────────────────────────────────────────────────────────────────────────
# 전망기(forecaster) — 모두 "train(=t 까지)" 만 보고 h개월치 분위예측을 낸다
# 반환: {h: {"median": float, "q": {level: float}, "mu": log평균, "sigma": log표준편차}}
# ─────────────────────────────────────────────────────────────────────────
def _q_from_lognormal(mu: float, sigma: float) -> dict[float, float]:
    return {lv: math.exp(mu + _N01.inv_cdf(lv) * sigma) for lv in QUANTILES}


def f_stat(train: pd.Series, cfg: Cfg) -> dict[int, dict]:
    """analyze.py calculate_statistical_bounds 재현: 감쇠 드리프트 + √h 변동성."""
    p_t = float(train.iloc[-1])
    lr = np.log(train / train.shift(1)).dropna()
    if len(lr) < 6:
        return {}
    sig_long = float(lr.iloc[-cfg.vol_long:].std(ddof=1))
    sig_short = float(lr.iloc[-cfg.vol_short:].std(ddof=1))
    sigma = cfg.vol_blend * sig_long + (1.0 - cfg.vol_blend) * sig_short
    sigma = max(sigma, cfg.vol_floor)
    drift = float(lr.iloc[-cfg.drift_win:].mean()) * cfg.drift_damping
    out = {}
    for h in cfg.horizons:
        mu = math.log(p_t) + drift * h
        sh = sigma * math.sqrt(h)
        out[h] = {"median": math.exp(mu), "q": _q_from_lognormal(mu, sh),
                  "mu": mu, "sigma": sh}
    return out


def f_naive(train: pd.Series, cfg: Cfg) -> dict[int, dict]:
    """랜덤워크(현재가 유지) + f_stat 과 같은 변동성 밴드. drift=0 케이스."""
    p_t = float(train.iloc[-1])
    lr = np.log(train / train.shift(1)).dropna()
    if len(lr) < 6:
        return {}
    sig_long = float(lr.iloc[-cfg.vol_long:].std(ddof=1))
    sig_short = float(lr.iloc[-cfg.vol_short:].std(ddof=1))
    sigma = max(cfg.vol_blend * sig_long + (1.0 - cfg.vol_blend) * sig_short, cfg.vol_floor)
    out = {}
    for h in cfg.horizons:
        mu = math.log(p_t)
        sh = sigma * math.sqrt(h)
        out[h] = {"median": p_t, "q": _q_from_lognormal(mu, sh), "mu": mu, "sigma": sh}
    return out


def f_drift_naive(train: pd.Series, cfg: Cfg) -> dict[int, dict]:
    """감쇠 없이(damping=1) 최근 drift_win 추세를 그대로 외삽 — '과신' 벤치마크."""
    p_t = float(train.iloc[-1])
    lr = np.log(train / train.shift(1)).dropna()
    if len(lr) < 6:
        return {}
    drift = float(lr.iloc[-cfg.drift_win:].mean())
    sigma = max(float(lr.iloc[-cfg.vol_long:].std(ddof=1)), cfg.vol_floor)
    out = {}
    for h in cfg.horizons:
        mu = math.log(p_t) + drift * h
        sh = sigma * math.sqrt(h)
        out[h] = {"median": math.exp(mu), "q": _q_from_lognormal(mu, sh), "mu": mu, "sigma": sh}
    return out


def f_seasonal_naive(series: pd.Series, t: int, cfg: Cfg) -> dict[int, dict]:
    """작년 같은 구간의 상대변화를 현재가에 적용: p_t * series[t-12+h]/series[t-12]."""
    if t - 12 < 0:
        return {}
    p_t = float(series.iloc[t])
    base_ly = float(series.iloc[t - 12])
    if base_ly <= 0:
        return {}
    lr = np.log(series.iloc[:t + 1] / series.iloc[:t + 1].shift(1)).dropna()
    sigma = max(float(lr.iloc[-cfg.vol_long:].std(ddof=1)), cfg.vol_floor)
    out = {}
    for h in cfg.horizons:
        j = t - 12 + h
        if j >= len(series):
            break
        ratio = float(series.iloc[j]) / base_ly
        med = p_t * ratio
        mu = math.log(max(med, 1e-9))
        sh = sigma * math.sqrt(h)
        out[h] = {"median": med, "q": _q_from_lognormal(mu, sh), "mu": mu, "sigma": sh}
    return out


# ─────────────────────────────────────────────────────────────────────────
# 채점
# ─────────────────────────────────────────────────────────────────────────
def pinball_pct(actual: float, qmap: dict[float, float], anchor: float) -> float:
    """분위손실(평균), anchor(=현재가) 대비 % 로 정규화 — 품목 간 비교 가능하게."""
    tot = 0.0
    for lv, q in qmap.items():
        diff = actual - q
        tot += (lv * diff) if diff >= 0 else ((lv - 1.0) * diff)
    return (tot / len(qmap)) / anchor * 100.0


def pit_value(actual: float, mu: float, sigma: float) -> float:
    """확률적분변환: 예측분포(로그정규) 하에서 F(actual). 캘리브레이션되면 ~U(0,1)."""
    if sigma <= 0 or actual <= 0:
        return float("nan")
    return _N01.cdf((math.log(actual) - mu) / sigma)


def dm_test(loss_m: np.ndarray, loss_b: np.ndarray, h: int) -> tuple[float, float]:
    """Diebold-Mariano (Harvey-Leybourne-Newbold 소표본 보정).
    반환 (DM*, p양측). DM<0 = 모델 손실 < 벤치 손실 = 모델이 더 낫다.
    h-step 예측은 최대 h-1 차 자기상관 → 그만큼 HAC 분산.
    """
    d = np.asarray(loss_m, float) - np.asarray(loss_b, float)
    d = d[np.isfinite(d)]
    n = len(d)
    if n < 8:
        return float("nan"), float("nan")
    dbar = d.mean()
    dc = d - dbar
    gamma0 = float(np.mean(dc * dc))
    s = gamma0
    for k in range(1, min(h, n)):
        s += 2.0 * float(np.mean(dc[k:] * dc[:-k]))
    if s <= 0:
        return float("nan"), float("nan")
    dm = dbar / math.sqrt(s / n)
    k_hln = math.sqrt(max((n + 1 - 2 * h + h * (h - 1) / n) / n, 1e-9))
    dm_star = dm * k_hln
    p = 2.0 * (1.0 - _N01.cdf(abs(dm_star)))
    return dm_star, p


def ks_uniform(pits: list[float]) -> tuple[float, float, int]:
    """PIT 표본이 U(0,1) 인지 KS 검정. 반환 (D, p근사, n)."""
    x = sorted(v for v in pits if math.isfinite(v))
    n = len(x)
    if n < 10:
        return float("nan"), float("nan"), n
    d = 0.0
    for i, v in enumerate(x, 1):
        d = max(d, abs(i / n - v), abs(v - (i - 1) / n))
    # 점근 p-value (Kolmogorov): Q(λ), λ = (√n + 0.12 + 0.11/√n) D
    lam = (math.sqrt(n) + 0.12 + 0.11 / math.sqrt(n)) * d
    p = 2.0 * sum((-1) ** (k - 1) * math.exp(-2.0 * (k * lam) ** 2) for k in range(1, 101))
    return d, min(max(p, 0.0), 1.0), n


# ─────────────────────────────────────────────────────────────────────────
# 워크포워드 루프
# ─────────────────────────────────────────────────────────────────────────
def run(monthly: dict[str, pd.Series], cfg: Cfg) -> list[dict]:
    recs: list[dict] = []
    for k, m in sorted(monthly.items()):
        n = len(m)
        # 전망 시점 t: min_train-1 부터, h개월 뒤 실제가가 존재하는 마지막까지
        for t in range(cfg.min_train - 1, n - 1):
            train = m.iloc[:t + 1]
            fm = f_stat(train, cfg)
            fn = f_naive(train, cfg)
            fd = f_drift_naive(train, cfg)
            fs = f_seasonal_naive(m, t, cfg)
            if not fm:
                continue
            f_month = m.index[t].strftime("%Y-%m")
            anchor = float(train.iloc[-1])
            for h in cfg.horizons:
                if t + h >= n or h not in fm:
                    continue
                actual = float(m.iloc[t + h])
                tgt_month = m.index[t + h].strftime("%Y-%m")
                mm = fm[h]
                err = (mm["median"] / actual - 1.0) * 100.0   # +면 전망이 실제보다 높았음(과대)
                q = mm["q"]
                rec = {
                    "commodity": k,
                    "forecast_month": f_month,
                    "target_month": tgt_month,
                    "horizon": h,
                    "anchor": round(anchor, 4),
                    "median": round(mm["median"], 4),
                    "q10": round(q[0.10], 4), "q25": round(q[0.25], 4),
                    "q75": round(q[0.75], 4), "q90": round(q[0.90], 4),
                    "actual": round(actual, 4),
                    "err_pct": round(err, 3),
                    "abs_err_pct": round(abs(err), 3),
                    "pinball_pct": round(pinball_pct(actual, q, anchor), 4),
                    "in80": bool(q[0.10] <= actual <= q[0.90]),
                    "in50": bool(q[0.25] <= actual <= q[0.75]),
                    "pit": round(pit_value(actual, mm["mu"], mm["sigma"]), 4),
                }
                for tag, fx in (("naive", fn), ("dnaive", fd), ("snaive", fs)):
                    b = fx.get(h) if fx else None
                    if b:
                        rec[f"{tag}_abs_err_pct"] = round(abs(b["median"] / actual - 1.0) * 100.0, 3)
                        rec[f"{tag}_pinball_pct"] = round(pinball_pct(actual, b["q"], anchor), 4)
                    else:
                        rec[f"{tag}_abs_err_pct"] = None
                        rec[f"{tag}_pinball_pct"] = None
                recs.append(rec)
    return recs


# ─────────────────────────────────────────────────────────────────────────
# 집계
# ─────────────────────────────────────────────────────────────────────────
def _mean(xs):
    xs = [x for x in xs if x is not None and math.isfinite(x)]
    return round(sum(xs) / len(xs), 4) if xs else None


def _agg(rows: list[dict], with_dm: int | None = None) -> dict:
    if not rows:
        return {"n": 0}
    pb_m = _mean([r["pinball_pct"] for r in rows])
    pb_n = _mean([r["naive_pinball_pct"] for r in rows])
    out = {
        "n": len(rows),
        "pinball_pct": pb_m,
        "mae_pct": _mean([r["abs_err_pct"] for r in rows]),
        "bias_pct": _mean([r["err_pct"] for r in rows]),   # +면 전망이 실제보다 높음(체계적 과대)
        "cov80": round(sum(r["in80"] for r in rows) / len(rows), 3),   # 목표 0.80
        "cov50": round(sum(r["in50"] for r in rows) / len(rows), 3),   # 목표 0.50
        "naive_pinball_pct": pb_n,
        "naive_mae_pct": _mean([r["naive_abs_err_pct"] for r in rows]),
        "dnaive_mae_pct": _mean([r["dnaive_abs_err_pct"] for r in rows]),
        "snaive_mae_pct": _mean([r["snaive_abs_err_pct"] for r in rows]),
        # 스킬: 1 - 모델/나이브. >0 이면 나이브보다 낫다(pinball 기준).
        "skill_vs_naive": (round(1.0 - pb_m / pb_n, 3) if pb_m and pb_n else None),
    }
    d, p, npit = ks_uniform([r["pit"] for r in rows])
    out["pit_ks_D"], out["pit_ks_p"], out["pit_n"] = (
        round(d, 4) if math.isfinite(d) else None,
        round(p, 4) if math.isfinite(p) else None, npit,
    )
    if with_dm:
        lm = np.array([r["pinball_pct"] for r in rows], float)
        lb = np.array([r["naive_pinball_pct"] if r["naive_pinball_pct"] is not None else np.nan
                       for r in rows], float)
        dm, dp = dm_test(lm, lb, with_dm)
        out["dm_vs_naive_stat"] = round(dm, 3) if math.isfinite(dm) else None
        out["dm_vs_naive_p"] = round(dp, 4) if math.isfinite(dp) else None
    return out


def pit_histogram(rows: list[dict], bins: int = 10) -> list[int]:
    h = [0] * bins
    for r in rows:
        v = r["pit"]
        if v is not None and math.isfinite(v):
            h[min(int(v * bins), bins - 1)] += 1
    return h


# ─────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description="통계 전망 워크포워드 백테스트")
    ap.add_argument("--data", default=DATA_FILE)
    ap.add_argument("--drift-damping", type=float, default=0.20)
    ap.add_argument("--drift-win", type=int, default=12)
    ap.add_argument("--vol-long", type=int, default=36)
    ap.add_argument("--vol-short", type=int, default=6)
    ap.add_argument("--vol-blend", type=float, default=0.5)
    ap.add_argument("--vol-floor", type=float, default=0.015)
    ap.add_argument("--min-train", type=int, default=36)
    ap.add_argument("--max-h", type=int, default=6)
    ap.add_argument("--no-csv", action="store_true", help="records.csv 미출력")
    a = ap.parse_args()
    cfg = Cfg(a)

    monthly = load_monthly(a.data)
    if not monthly:
        raise SystemExit(f"[에러] {a.data} 에서 월별 시계열을 못 만들었습니다.")
    span = {k: f"{m.index[0]:%Y-%m}~{m.index[-1]:%Y-%m} ({len(m)}개월)"
            for k, m in monthly.items()}

    recs = run(monthly, cfg)
    if not recs:
        raise SystemExit("[에러] 채점된 전망이 0건입니다(데이터 부족).")

    by_c, by_h = {}, {}
    for r in recs:
        by_c.setdefault(r["commodity"], []).append(r)
        by_h.setdefault(r["horizon"], []).append(r)

    results = {
        "generated_at": datetime.now(_KST).strftime("%Y-%m-%d %H:%M"),
        "method": ("통계 단독(감쇠 드리프트 + √h 로그정규 밴드). analyze.py "
                   "calculate_statistical_bounds 재현. Gemini 블렌드 제외 = 스킬 하한."),
        "config": cfg.as_dict(),
        "data": {"file": a.data, "commodities": len(monthly), "span": span},
        "n_scored": len(recs),
        "overall": _agg(recs, with_dm=cfg.max_h),
        "by_horizon": {str(h): _agg(by_h[h], with_dm=h) for h in sorted(by_h)},
        "by_commodity": {k: _agg(by_c[k], with_dm=cfg.max_h) for k in sorted(by_c)},
        "pit_histogram_overall": pit_histogram(recs),
        "pit_note": ("10칸 균등이면 캘리브레이션 양호. 가운데가 높으면 밴드가 과대(과신 부족), "
                     "양 끝이 높으면 밴드가 과소(과신)."),
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(f"{OUT_DIR}/results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    if not a.no_csv:
        cols = list(recs[0].keys())
        with open(f"{OUT_DIR}/records.csv", "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(recs)

    def _s(v):  # None 안전 표시
        return "  -  " if v is None else f"{v}"

    o = results["overall"]
    print(f"[백테스트] {len(monthly)}품목 · {len(recs)}건 채점")
    print(f"  pinball%={_s(o['pinball_pct'])}  MAE%={_s(o['mae_pct'])}  bias%={_s(o['bias_pct'])}")
    print(f"  커버리지 80={_s(o['cov80'])}(목표 .80)  50={_s(o['cov50'])}(목표 .50)")
    print(f"  나이브대비 스킬(pinball)={_s(o['skill_vs_naive'])}  "
          f"DM*={_s(o.get('dm_vs_naive_stat'))} p={_s(o.get('dm_vs_naive_p'))}")
    print(f"  PIT KS D={_s(o['pit_ks_D'])} p={_s(o['pit_ks_p'])}")
    print(f"  → {OUT_DIR}/results.json" + ("" if a.no_csv else f" · {OUT_DIR}/records.csv"))
    for h in sorted(by_h):
        ah = results["by_horizon"][str(h)]
        print(f"   h={h}: MAE%={_s(ah['mae_pct']):>7}  skill={_s(ah['skill_vs_naive']):>7}  "
              f"cov80={_s(ah['cov80']):>6}  DMp={_s(ah.get('dm_vs_naive_p'))}")


if __name__ == "__main__":
    main()


# ═════════════════════════════════════════════════════════════════════════
# 가정 · 근거  (이 파일이 하는 모든 통계적 선택의 이유와 한계)
# ═════════════════════════════════════════════════════════════════════════
#
# [전체 설계 — 왜 워크포워드인가]
#   · 과거 특정 시점 t 에서 "그때까지 알 수 있던 데이터만" 으로 전망하고 이후
#     실현치로 채점 = look-ahead bias(미래참조 편향) 없는 유일한 out-of-sample
#     평가. in-sample fit(R² 등)은 과적합을 못 걸러 전망 신뢰도 근거가 못 된다.
#   · t 를 한 달씩 굴리며 겹치는 예측을 모두 채점(expanding window). 한 번의
#     hold-out 보다 표본이 크고, DM 검정에 필요한 손실차 시계열을 준다.
#   · Gemini 블렌드는 (a)과거 프롬프트·뉴스를 재현 불가 (b)비결정적·유료 라
#     제외. 여기서 재는 것은 "통계 뼈대" 의 스킬이고, 실제 전망은 그 위에
#     AI 를 50:50 블렌드하므로 실제 스킬은 이 값 근처에서 시작한다(하한 성격).
#
# [월말 리샘플 (resample 'ME' last)]
#   · 전망 대상이 "월별 경로" 라 일봉을 월말 종가로 축약. 월중 고저는 버리지만
#     6개월 전망의 평가엔 월말 수준이 기준. analyze._monthly_series 와 동일.
#   · 최소 24개월 미만 시계열은 제외(드리프트·변동성 추정이 불안정).
#
# [모델: 기하 랜덤워크 + 감쇠 드리프트]
#   · 가격이 아니라 로그가격을 모델링 → 항상 양수, 수익률이 대칭. 원자재
#     표준 관행.
#   · μ_h = ln(P_t) + (damping · mean_{drift_win} r) · h.
#     - 최근 평균 로그수익률 r 을 그대로 h배 외삽하면 단기 추세를 6개월
#       미래로 과신 투사(추세추종의 전형적 실패). damping<1 로 그 정도를 깎음.
#     - 기본 damping=0.20, drift_win=12: analyze.py 현행값. **이 값이 최적이라는
#       근거는 아직 없음** — 본 백테스트로 damping∈{0,.1,.2,.3,.5}, drift_win∈
#       {6,12,18} 스윕해 pinball 최소값을 찾는 것이 단계 C 의 입력.
#     - damping=0 이면 f_naive(순수 랜덤워크), damping=1 이면 f_drift_naive.
#       둘을 벤치마크로 같이 채점해 "감쇠가 실제로 값어치 있나" 를 본다.
#
# [밴드: √h 스케일 로그정규 분위]
#   · σ_h = σ_1 · √h  (독립증분 랜덤워크 가정). 변동성이 평균회귀하거나
#     군집(volatility clustering)하면 틀림 — 특히 h 가 커질수록 과소·과대가
#     누적된다. GARCH term-structure 로의 교체는 단계 F 과제이고, 그 개선
#     여부도 이 백테스트의 cov80/PIT 로 판정한다.
#   · σ_1 = blend·std_{vol_long} + (1-blend)·std_{vol_short}, 하한 vol_floor.
#     - 장기(36m)만 쓰면 최근 급변에 둔감, 단기(6m)만 쓰면 노이즈에 출렁.
#       50:50 은 analyze.py 현행값이며 역시 스윕 대상(vol_blend, vol_long,
#       vol_short, vol_floor).
#     - 표본표준편차 ddof=1 (analyze.py 와 동일).
#   · 분위는 정규분포 역CDF(NormalDist.inv_cdf). 0.10/0.90 = analyze.py 의
#     z=1.28(80% 구간)과 정확히 일치하도록 QUANTILES 를 잡음.
#
# [채점 지표 — 왜 이 조합인가]
#   · pinball loss(=quantile loss) 평균: 분위예측 전체에 대한 proper scoring
#     rule. 점추정만 보는 MAE 와 달리 "밴드의 폭까지" 벌점한다. anchor(=현재가)
#     대비 %로 정규화해 WTI(달러)와 텅스텐(위안)을 같은 척도로 합산 가능.
#   · MAE% / bias%: 해석용. bias>0 = 전망이 실제보다 체계적으로 높음(과대전망)
#     → 단계 C 의 편향 보정량 산출 근거.
#   · cov80 / cov50: 밴드가 명목 확률을 실제로 담는가(경험적 커버리지).
#     0.80 밴드가 0.6 만 담으면 과신 → 단계 C 의 밴드 캘리브레이션 배율 근거.
#   · PIT(확률적분변환) + KS: 커버리지보다 엄격한 분포 캘리브레이션 검사.
#     예측분포가 맞으면 PIT ~ U(0,1). 히스토그램 가운데 볼록=밴드 과대,
#     U자=밴드 과소, 우상향=전망 과소편향, 우하향=과대편향.
#   · 나이브/드리프트나이브/계절나이브 대비 스킬 = 1 - loss_model/loss_bench.
#     "전망이 값어치 있으려면 최소한 '현재가 유지' 를 이겨야 한다" 는 기준선.
#     원자재 6개월 예측에서 나이브를 꾸준히 이기기는 실제로 어렵고, 못 이기면
#     그 사실을 정직하게 보고하는 것이 목적(설득력의 근거).
#
# [Diebold-Mariano 검정 (HLN 보정)]
#   · 스킬 차이가 "우연" 인지 검정. 손실차 d_t = loss_model - loss_naive 의
#     평균이 0 이라는 귀무가설. h-step 예측은 겹쳐서 최대 h-1 차 자기상관을
#     가지므로 HAC(분산에 자기공분산 2·Σγ_k 가산)로 표준오차를 키운다.
#   · Harvey-Leybourne-Newbold 소표본 보정계수 K = √((n+1-2h+h(h-1)/n)/n) 를
#     곱함(원 DM 은 소표본에서 기각 과다). p 는 정규근사 양측.
#   · |DM*|>~1.96, p<0.05 이면 스킬 차이가 통계적으로 유의. n(겹침 고려 유효
#     표본)이 작으면 유의하게 나오기 어렵고, 그것도 정보다.
#
# [알려진 한계 / 다음 단계에서 다룰 것]
#   · expanding window 라 초기 시점은 훈련량이 적다(min_train=36 로 하한).
#     rolling window 옵션은 추후.
#   · 품목 독립 채점 — 원자재 간 상관(특히 비철금속)은 무시. 단계 E(공통인자).
#   · 계절나이브는 "작년 한 해" 만 참조 — 다년 계절 평균/푸리에는 추후.
#   · history_3y 는 CSV(표시단위) 우선이라 과거 개정·소급수정이 있으면 그
#     버전이 채점에 반영된다(실시간 vintage 아님). 방향성 평가엔 영향 작음.
#   · steel 은 2020-09~ 데이터라 표본이 절반 → by_commodity steel 수치는
#     신뢰구간이 넓다(n 을 함께 볼 것).
# ═════════════════════════════════════════════════════════════════════════

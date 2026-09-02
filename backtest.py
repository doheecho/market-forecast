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
        # 단계 D: 앙상블 멤버
        self.mr_win = a.mr_win                 # 평균회귀: 장기평균 창(개월)
        self.mom_win = a.mom_win               # 모멘텀: OLS 기울기 창(개월)
        self.mom_damping = a.mom_damping       # 모멘텀 기울기 감쇠
        # 단계 F: 변동성 term-structure
        self.vol_model = a.vol_model           # 'sqrt'(현행 √h) | 'garch'(GARCH(1,1) 누적)

    def as_dict(self) -> dict:
        return {
            "drift_damping": self.drift_damping, "drift_win": self.drift_win,
            "vol_long": self.vol_long, "vol_short": self.vol_short,
            "vol_blend": self.vol_blend, "vol_floor": self.vol_floor,
            "min_train": self.min_train, "max_h": self.max_h,
            "mr_win": self.mr_win, "mom_win": self.mom_win,
            "mom_damping": self.mom_damping, "vol_model": self.vol_model,
            "quantiles": list(QUANTILES),
        }


def _blend_sigma(train: pd.Series, cfg: Cfg) -> float:
    """월간 로그수익률 표준편차 = blend·장기 + (1-blend)·단기, 하한 vol_floor."""
    lr = np.log(train / train.shift(1)).dropna()
    if len(lr) < 6:
        return cfg.vol_floor
    sl = float(lr.iloc[-cfg.vol_long:].std(ddof=1))
    ss = float(lr.iloc[-cfg.vol_short:].std(ddof=1))
    return max(cfg.vol_blend * sl + (1.0 - cfg.vol_blend) * ss, cfg.vol_floor)


# ── 단계 F: GARCH(1,1) 변동성 term-structure ──────────────────────────
# √h 는 "월간 분산이 일정" 가정 — 실제로는 변동성이 평균회귀·군집한다.
# GARCH(1,1): σ²_t = ω + α·ε²_{t-1} + β·σ²_{t-1}. 분산타게팅으로 ω = v̄(1-α-β)
# 고정, (α,β)는 조립그리드에서 가우시안 로그우도 최대화(scipy 불필요).
# h개월 누적분산(=h개월 뒤 로그가격 수준의 분산):
#   Σ_{k=1..h} E[σ²_{t+k}] = h·v̄ + (σ²_{t+1}-v̄)·(1-(α+β)^h)/(1-(α+β))
# → σ_h = √(누적분산).  α+β<1 이면 h 가 커질수록 선형(√h 아님)에 수렴.
_GA = tuple(round(x, 2) for x in np.arange(0.02, 0.301, 0.03))   # α 그리드
_GB = tuple(round(x, 2) for x in np.arange(0.60, 0.951, 0.03))   # β 그리드


def _garch_sigma_path(lr: np.ndarray, max_h: int, floor: float) -> np.ndarray:
    """월간 로그수익률 배열 → [σ_1..σ_max_h] (h개월 누적 로그표준편차)."""
    r = lr - float(lr.mean())
    n = len(r)
    v_bar = float(np.var(lr, ddof=1))
    fb = np.array([max(math.sqrt(v_bar), floor) * math.sqrt(h)
                   for h in range(1, max_h + 1)])
    if n < 24 or v_bar <= 0:
        return fb
    r2 = r * r
    best = None
    for al in _GA:
        for be in _GB:
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
                ll -= 0.5 * (math.log(s2) + r2[t] / s2)
            if ok and (best is None or ll > best[0]):
                best = (ll, al, be, s2)
    if best is None:
        return fb
    _, al, be, s2_last = best
    ab = al + be
    s2_next = v_bar * (1.0 - ab) + al * r2[-1] + be * s2_last     # σ²_{t+1}
    out = []
    for h in range(1, max_h + 1):
        geo = h if abs(1.0 - ab) < 1e-9 else (1.0 - ab ** h) / (1.0 - ab)
        cum = h * v_bar + (s2_next - v_bar) * geo
        out.append(math.sqrt(max(cum, (floor ** 2) * h)))
    return np.array(out)


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


def _factor_sigma_path(lr: pd.Series, cfg: Cfg, panel: pd.DataFrame, key: str) -> np.ndarray | None:
    """단계 E: 공통 변동성 팩터. r_i = β_i·F + ε_i (F = 12종 등가중 월간 로그수익률
    '원자재지수'). σ²_{i,h} = β_i²·Var(F_h) + Var(ε_i,h), 각 항은 GARCH 누적.
    → 공통 국면(2020·2022 등)에 모든 밴드가 함께 팽창. 불가 시 None(→ GARCH 폴백)."""
    if panel is None or key not in panel.columns:
        return None
    P = panel.loc[:lr.index[-1]].dropna(how="all")
    F = P.mean(axis=1, skipna=True).dropna()
    ri = P[key].dropna()
    idx = F.index.intersection(ri.index)
    if len(idx) < 30:
        return None
    Fv, rv = F.loc[idx].to_numpy(), ri.loc[idx].to_numpy()
    vF = float(np.var(Fv, ddof=1))
    if vF <= 0:
        return None
    beta = float(np.cov(rv, Fv, ddof=1)[0, 1] / vF)
    eps = rv - beta * Fv
    sF = _garch_sigma_path(Fv, cfg.max_h, cfg.vol_floor)      # 누적 stdev
    sE = _garch_sigma_path(eps, cfg.max_h, cfg.vol_floor)
    out = np.sqrt((beta ** 2) * sF ** 2 + sE ** 2)
    floor = np.array([cfg.vol_floor * math.sqrt(h) for h in range(1, cfg.max_h + 1)])
    return np.maximum(out, floor)


def f_stat(train: pd.Series, cfg: Cfg, panel: pd.DataFrame | None = None,
           key: str | None = None) -> dict[int, dict]:
    """감쇠 드리프트 + 변동성 term-structure. vol_model: sqrt=σ_1·√h(현행),
    garch=GARCH(1,1) 누적(단계 F), factor=공통 변동성 팩터(단계 E)."""
    p_t = float(train.iloc[-1])
    lr = np.log(train / train.shift(1)).dropna()
    if len(lr) < 6:
        return {}
    sig_long = float(lr.iloc[-cfg.vol_long:].std(ddof=1))
    sig_short = float(lr.iloc[-cfg.vol_short:].std(ddof=1))
    sigma = max(cfg.vol_blend * sig_long + (1.0 - cfg.vol_blend) * sig_short, cfg.vol_floor)
    drift = float(lr.iloc[-cfg.drift_win:].mean()) * cfg.drift_damping
    sig_h = None
    if cfg.vol_model == "factor":
        sig_h = _factor_sigma_path(lr, cfg, panel, key)
    if sig_h is None and cfg.vol_model in ("garch", "factor"):
        sig_h = _garch_sigma_path(lr.to_numpy(), cfg.max_h, cfg.vol_floor)
    if sig_h is None:
        sig_h = np.array([sigma * math.sqrt(h) for h in range(1, cfg.max_h + 1)])
    out = {}
    for h in cfg.horizons:
        mu = math.log(p_t) + drift * h
        sh = float(sig_h[h - 1])
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


def f_meanrev(train: pd.Series, cfg: Cfg) -> dict[int, dict]:
    """평균회귀(AR(1) on 로그가격 편차). 장기평균 ma 로 φ^h 속도로 회귀.
    mu_h = ma + φ^h·(lnP_t - ma). φ 는 편차의 lag-1 자기회귀계수(0~0.999 클립)."""
    lp = np.log(train.values.astype(float))
    if len(lp) < cfg.mr_win + 3:
        return {}
    ma = float(lp[-cfg.mr_win:].mean())
    dev = lp - ma
    x, y = dev[:-1], dev[1:]
    denom = float(np.dot(x, x))
    phi = float(np.dot(x, y) / denom) if denom > 0 else 0.0
    phi = min(max(phi, 0.0), 0.999)
    sigma = _blend_sigma(train, cfg)
    lp_t = float(lp[-1])
    out = {}
    for h in cfg.horizons:
        mu = ma + (phi ** h) * (lp_t - ma)
        sh = sigma * math.sqrt(h)
        out[h] = {"median": math.exp(mu), "q": _q_from_lognormal(mu, sh), "mu": mu, "sigma": sh}
    return out


def f_momentum(train: pd.Series, cfg: Cfg) -> dict[int, dict]:
    """모멘텀: 최근 mom_win 개월 로그가격의 OLS 기울기 β 를 감쇠해 외삽.
    mu_h = lnP_t + (mom_damping·β)·h."""
    lp = np.log(train.values.astype(float))[-cfg.mom_win:]
    if len(lp) < 4:
        return {}
    xs = np.arange(len(lp), dtype=float)
    beta = float(np.polyfit(xs, lp, 1)[0]) * cfg.mom_damping
    sigma = _blend_sigma(train, cfg)
    lp_t = float(np.log(train.iloc[-1]))
    out = {}
    for h in cfg.horizons:
        mu = lp_t + beta * h
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
def _build_panel(monthly: dict[str, pd.Series]) -> pd.DataFrame:
    """12종 월간 로그수익률 패널 (index 합집합, 결측 NaN)."""
    return pd.DataFrame({k: np.log(m / m.shift(1)) for k, m in monthly.items()})


def run(monthly: dict[str, pd.Series], cfg: Cfg,
        panel: pd.DataFrame | None = None) -> list[dict]:
    recs: list[dict] = []
    if panel is None and cfg.vol_model == "factor":
        panel = _build_panel(monthly)
    for k, m in sorted(monthly.items()):
        n = len(m)
        # 전망 시점 t: min_train-1 부터, h개월 뒤 실제가가 존재하는 마지막까지
        for t in range(cfg.min_train - 1, n - 1):
            train = m.iloc[:t + 1]
            fm = f_stat(train, cfg, panel, k)
            fn = f_naive(train, cfg)
            fd = f_drift_naive(train, cfg)
            fs = f_seasonal_naive(m, t, cfg)
            fr = f_meanrev(train, cfg)       # 단계 D
            fmo = f_momentum(train, cfg)     # 단계 D
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
                    "_mu": mm["mu"], "_sigma": mm["sigma"],   # 재캘리브레이션용(내부)
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
                # 단계 D: 각 멤버의 median(로그) 저장 — 앙상블은 후처리에서 결합
                for name, fx in (("stat", fm), ("naive", fn), ("drift", fd),
                                 ("season", fs), ("meanrev", fr), ("mom", fmo)):
                    b = fx.get(h) if fx else None
                    rec[f"_m_{name}"] = math.log(b["median"]) if b and b["median"] > 0 else None
                recs.append(rec)
    return recs


# ─────────────────────────────────────────────────────────────────────────
# 단계 D: 앙상블 (멤버 median 결합 → C 밴드 유지)
#   멤버: stat(감쇠드리프트) · naive(랜덤워크) · drift(무감쇠) · season(계절) ·
#         meanrev(평균회귀) · mom(모멘텀). 앙상블 중심 = 가중 기하평균(로그공간
#         median 의 가중평균). 밴드는 f_stat 의 sigma + (C 있으면) conformal Qz.
#   가중: equal 또는 invmae(롤링 OOS · member×horizon 별 1/MAE, equal 로 shrink).
# ─────────────────────────────────────────────────────────────────────────
_MEMBERS = ("stat", "naive", "drift", "season", "meanrev", "mom")


def _ens_record(r: dict, w_by_h: dict, calib: dict | None) -> dict:
    """r 의 멤버 median 을 w(해당 horizon) 로 결합한 앙상블 record."""
    h = r["horizon"]
    w = w_by_h.get(h) or {n: 1.0 for n in _MEMBERS}
    num = den = 0.0
    for n in _MEMBERS:
        lm = r.get(f"_m_{n}")
        if lm is not None and w.get(n, 0) > 0:
            num += w[n] * lm
            den += w[n]
    if den <= 0:
        return dict(r)
    mu = num / den
    sig = r["_sigma"]
    qzb = (calib or {}).get(h)                          # (z_lo, z_hi) or None
    if qzb:
        # C 와 동일: band-only conformal. 중간분위는 정규, 0.1/0.9 는 실측.
        zc = {0.05: -1.6449, 0.10: qzb[0], 0.25: -0.6745, 0.50: 0.0,
              0.75: 0.6745, 0.90: qzb[1], 0.95: 1.6449}
        q = {lv: math.exp(mu + zc[lv] * sig) for lv in QUANTILES}
    else:
        q = _q_from_lognormal(mu, sig)
    actual, anchor = r["actual"], r["anchor"]
    err = (math.exp(mu) / actual - 1.0) * 100.0
    out = dict(r)
    out.update({
        "_mu": mu, "median": round(math.exp(mu), 4),
        "q10": round(q[0.10], 4), "q25": round(q[0.25], 4),
        "q75": round(q[0.75], 4), "q90": round(q[0.90], 4),
        "err_pct": round(err, 3), "abs_err_pct": round(abs(err), 3),
        "pinball_pct": round(pinball_pct(actual, q, anchor), 4),
        "in80": bool(q[0.10] <= actual <= q[0.90]),
        "in50": bool(q[0.25] <= actual <= q[0.75]),
        "pit": round(pit_value(actual, mu, sig), 4),
    })
    return out


def _member_abs_err(r: dict, name: str) -> float | None:
    lm = r.get(f"_m_{name}")
    if lm is None:
        return None
    return abs(math.exp(lm) / r["actual"] - 1.0) * 100.0


def ensemble_equal(records: list[dict], calib: dict | None) -> list[dict]:
    return [_ens_record(r, {}, calib) for r in records]


def _weights_from_pool(err_pool: dict, all_h, shrink: float, warmup: int) -> dict:
    w_by_h = {}
    eq = 1.0 / len(_MEMBERS)
    for h in all_h:
        ws = {}
        for n in _MEMBERS:
            es = err_pool.get((h, n), [])
            if len(es) >= warmup:
                ws[n] = 1.0 / max(sum(es) / len(es), 1e-6)
        if ws:
            s = sum(ws.values())
            w_by_h[h] = {n: (1 - shrink) * (ws.get(n, 0.0) / s) + shrink * eq
                         for n in _MEMBERS}
    return w_by_h


def ensemble_invmae(records: list[dict], calib: dict | None,
                    shrink: float, warmup: int) -> list[dict]:
    """롤링 OOS: 각 전망월의 가중치는 그 이전에 실현된 멤버별 오차로만 적합."""
    recs = sorted(records, key=lambda r: (r["forecast_month"], r["commodity"], r["horizon"]))
    all_h = sorted({r["horizon"] for r in recs})
    err_pool: dict[tuple[int, str], list[float]] = {}
    staged: list[tuple[str, int, dict]] = []
    out, cur_fm, w_by_h = [], None, {}
    for r in recs:
        fm = r["forecast_month"]
        if fm != cur_fm:
            keep = []
            for tm, h, rr in staged:
                if tm < fm:
                    for n in _MEMBERS:
                        e = _member_abs_err(rr, n)
                        if e is not None:
                            err_pool.setdefault((h, n), []).append(e)
                else:
                    keep.append((tm, h, rr))
            staged, cur_fm = keep, fm
            w_by_h = _weights_from_pool(err_pool, all_h, shrink, warmup)
        out.append(_ens_record(r, w_by_h, calib))
        staged.append((r["target_month"], r["horizon"], r))
    return out


def emit_ensemble(records: list[dict], shrink: float, warmup: int) -> dict:
    """전체 이력으로 horizon별 최종 앙상블 가중치 → ensemble.json."""
    by_hn: dict[tuple[int, str], list[float]] = {}
    for r in records:
        for n in _MEMBERS:
            e = _member_abs_err(r, n)
            if e is not None:
                by_hn.setdefault((r["horizon"], n), []).append(e)
    hs = sorted({h for (h, _) in by_hn})
    fit = {}
    for h in hs:
        ws = {}
        for n in _MEMBERS:
            es = by_hn.get((h, n), [])
            if len(es) >= warmup:
                ws[n] = 1.0 / max(sum(es) / len(es), 1e-6)
        if not ws:
            continue
        s = sum(ws.values())
        inv = {n: ws.get(n, 0.0) / s for n in _MEMBERS}
        eq = 1.0 / len(_MEMBERS)
        fit[str(h)] = {n: round((1 - shrink) * inv[n] + shrink * eq, 4) for n in _MEMBERS}
    return {
        "generated_at": datetime.now(_KST).strftime("%Y-%m-%d %H:%M"),
        "method": ("forecast combination. 앙상블 중심 = Σ w_n·ln(median_n) / Σ w_n "
                   "(로그공간 가중평균 = 가중 기하평균). w_n ∝ 1/MAE_n (horizon별), "
                   f"equal({eq:.3f})로 shrink={shrink}. 밴드는 f_stat sigma + calibration.json."),
        "members": list(_MEMBERS),
        "shrink": shrink, "warmup": warmup,
        "by_horizon": fit,
        "note": ("analyze.py 가 이 가중치로 통계 중심선을 만들고, calibration.json 으로 "
                 "밴드를 뽑는다. 파일 없으면 f_stat 단독(=단계 C 상태)."),
    }


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
# 단계 C: split-conformal 재캘리브레이션
#   모델은 ln A ~ N(mu, sigma²) 로 분위를 Φ⁻¹(p)·sigma 로 뽑는다. 실제로는
#   표준화 잔차 z=(lnA-mu)/sigma 가 정규가 아니다(팩테일·비대칭·중앙값≠0).
#   → Φ⁻¹(p) 자리에 **z 의 실측 p-분위 Qz(p)** 를 그대로 꽂는다(split conformal
#     prediction). 커버리지가 구성상 목표에 맞고, 팩테일이면 80% 밴드만 넓어지고
#     50% 는 유지되며, 상방/하방 비대칭도 자동 반영. Qz(0.5)≠0 이면 그게 곧
#     (평균이 아닌 중앙값 기반의) 견고한 편향 보정.
#   식: q_p = exp( mu + Qz_h(p) · sigma ),  base = q_0.5, bull = q_0.9, bear = q_0.1
#   보정 안 함(파일 없음/워밍업 전) = Qz_h(p) → Φ⁻¹(p) 로 항등.
# ─────────────────────────────────────────────────────────────────────────
def _emp_quantiles(z: np.ndarray) -> dict:
    return {lv: round(float(np.quantile(z, lv)), 4) for lv in QUANTILES}


def load_calibration_local(path: str = "calibration.json") -> dict:
    """calibration.json → {horizon(int): (z_lo, z_hi)}. 없으면 {} (정규 항등).
    _common.load_calibration 과 동형이지만 backtest 는 _common(=yfinance) 을
    import 안 하려고 별도 구현."""
    try:
        d = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for hs, v in (d.get("by_horizon") or {}).items():
        qb = (v or {}).get("qz_band") or {}
        try:
            lo, hi = float(qb["0.1"]), float(qb["0.9"])
        except (KeyError, TypeError, ValueError):
            continue
        if lo < 0 < hi:
            out[int(hs)] = (lo, hi)
    return out


def _recal_record(r: dict, qz: dict | None, poolz: np.ndarray | None,
                  center: str = "model") -> dict:
    """r 을 경험분위 qz 로 재계산. qz=None 이면 원본 그대로.
    center='model': 중앙값은 모델값 유지(qz 를 median 으로 recenter) — 스프레드만 보정.
    center='conformal': qz(0.5) 도 그대로 적용(중앙값까지 이동 = 편향 보정 포함)."""
    if not qz:
        return dict(r)
    mu, sig = r["_mu"], r["_sigma"]
    shift = qz[0.50] if center == "model" else 0.0
    q2 = {lv: math.exp(mu + (qz[lv] - shift) * sig) for lv in QUANTILES}
    med2, actual, anchor = q2[0.50], r["actual"], r["anchor"]
    err = (med2 / actual - 1.0) * 100.0
    if poolz is not None and len(poolz):
        za = (math.log(actual) - mu) / sig + shift if sig > 0 else float("nan")
        pit2 = (float(np.count_nonzero(poolz <= za)) + 0.5) / len(poolz)
    else:
        pit2 = pit_value(actual, mu, sig)
    out = dict(r)
    out.update({
        "median": round(med2, 4),
        "q10": round(q2[0.10], 4), "q25": round(q2[0.25], 4),
        "q75": round(q2[0.75], 4), "q90": round(q2[0.90], 4),
        "err_pct": round(err, 3), "abs_err_pct": round(abs(err), 3),
        "pinball_pct": round(pinball_pct(actual, q2, anchor), 4),
        "in80": bool(q2[0.10] <= actual <= q2[0.90]),
        "in50": bool(q2[0.25] <= actual <= q2[0.75]),
        "pit": round(pit2, 4),
    })
    return out


def calibrate_rolling(records: list[dict], warmup: int, center: str = "model") -> list[dict]:
    """진짜 out-of-sample: 각 전망을 그 전망월 이전에 '이미 실현된' 잔차의
    경험분위로만 재계산. 워밍업(잔차 warmup개) 전에는 항등."""
    recs = sorted(records, key=lambda r: (r["forecast_month"], r["commodity"], r["horizon"]))
    pool: dict[int, list[float]] = {}
    staged: list[tuple[str, int, float]] = []
    out, cur_fm = [], None
    for r in recs:
        fm = r["forecast_month"]
        if fm != cur_fm:
            keep = []
            for tm, h, z in staged:
                (pool.setdefault(h, []).append(z) if tm < fm else keep.append((tm, h, z)))
            staged, cur_fm = keep, fm
        h = r["horizon"]
        pz = np.array(pool.get(h, []), float)
        qz = _emp_quantiles(pz) if len(pz) >= warmup else None
        out.append(_recal_record(r, qz, pz if qz else None, center))
        if r["_sigma"] > 0:
            staged.append((r["target_month"], h,
                           (math.log(r["actual"]) - r["_mu"]) / r["_sigma"]))
    return out


def emit_calibration(records: list[dict], warmup: int) -> dict:
    """전체 이력으로 horizon별 경험분위 Qz(p) 산출 → calibration.json.
    production(analyze.py)이 Φ⁻¹(p) 대신 이 값을 써서 통계 밴드를 뽑는다."""
    by_h: dict[int, list[float]] = {}
    for r in records:
        if r["_sigma"] > 0:
            by_h.setdefault(r["horizon"], []).append(
                (math.log(r["actual"]) - r["_mu"]) / r["_sigma"])
    fit = {}
    for h in sorted(by_h):
        z = np.array(by_h[h], float)
        if len(z) >= warmup:
            qz = _emp_quantiles(z)
            med = qz[0.50]
            fit[str(h)] = {
                "n": len(z),
                "qz": qz,                                       # 원본 경험분위
                "qz_band": {str(lv): round(qz[lv] - med, 4)     # 중앙값 recenter(스프레드만)
                            for lv in QUANTILES},
            }
    return {
        "generated_at": datetime.now(_KST).strftime("%Y-%m-%d %H:%M"),
        "method": ("split-conformal. 통계 밴드의 분위계수를 Φ⁻¹(p) 대신 표준화잔차 "
                   "z=(lnA-mu)/sigma 의 실측 p-분위로 대체."),
        "center": "model",
        "apply": ("production 은 qz_band 를 쓴다: base = exp(mu)(모델 중앙값 유지), "
                  "bull = exp(mu + qz_band['0.9']·sigma), bear = exp(mu + qz_band['0.1']·sigma). "
                  "백테스트상 중앙값까지 옮기면(qz) 국면 편향을 좇아 MAE·pinball 악화."),
        "quantile_levels": list(QUANTILES),
        "phi_inv_reference": {str(lv): round(_N01.inv_cdf(lv), 4) for lv in QUANTILES},
        "by_horizon": fit,
        "note": ("qz_band['0.9'] > 1.2816 = 상방 밴드 확대, qz_band['0.1'] < -1.2816 = "
                 "하방 확대(비대칭 허용). 파일 없거나 특정 h 없으면 그 h 는 Φ⁻¹(p) 로 항등."),
    }


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
    ap.add_argument("--vol-model", choices=("sqrt", "garch", "factor"), default="sqrt",
                    help="밴드 변동성: sqrt=σ_1·√h(현행), garch=GARCH 누적(F), factor=공통변동성팩터(E)")
    ap.add_argument("--compare-garch", action="store_true",
                    help="단계 F: sqrt vs garch 를 한 번에 비교(+calibration)")
    ap.add_argument("--compare-factor", action="store_true",
                    help="단계 E: garch vs factor 를 한 번에 비교(+calibration)")
    ap.add_argument("--no-csv", action="store_true", help="records.csv 미출력")
    ap.add_argument("--calibrate", action="store_true",
                    help="단계 C: 롤링 OOS split-conformal 재캘리브레이션 결과도 산출·비교")
    ap.add_argument("--cal-warmup", type=int, default=40,
                    help="이 개수 이상 실현 잔차가 쌓여야 그 horizon 캘리브레이션 시작")
    ap.add_argument("--center", choices=("model", "conformal"), default="model",
                    help="model=중앙값은 모델값 유지(스프레드만 보정), conformal=중앙값도 이동")
    ap.add_argument("--emit-calibration", action="store_true",
                    help="전체 이력으로 calibration.json (production 이 읽을 파일) 생성")
    # 단계 D: 앙상블
    ap.add_argument("--mr-win", type=int, default=24, help="평균회귀 장기평균 창(개월)")
    ap.add_argument("--mom-win", type=int, default=6, help="모멘텀 OLS 기울기 창(개월)")
    ap.add_argument("--mom-damping", type=float, default=0.5, help="모멘텀 기울기 감쇠")
    ap.add_argument("--ensemble", action="store_true",
                    help="단계 D: equal·invmae 앙상블 결과도 산출·비교 (calibration.json 위에서)")
    ap.add_argument("--ens-shrink", type=float, default=0.3, help="invmae 가중을 equal 로 shrink")
    ap.add_argument("--ens-warmup", type=int, default=24,
                    help="member×horizon 별 실현오차 이 개수 이상이어야 invmae 가중 적용")
    ap.add_argument("--emit-ensemble", action="store_true",
                    help="전체 이력으로 ensemble.json (production 가중치) 생성")
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

    # ── 단계 C: 롤링 OOS 재캘리브레이션 비교 ──
    cal_recs = None
    if a.calibrate:
        cal_recs = calibrate_rolling(recs, a.cal_warmup, a.center)
        cby_h = {}
        for r in cal_recs:
            cby_h.setdefault(r["horizon"], []).append(r)
        results["calibration_run"] = {
            "params": {"method": "split-conformal (rolling OOS)",
                       "warmup": a.cal_warmup, "center": a.center},
            "overall": _agg(cal_recs, with_dm=cfg.max_h),
            "by_horizon": {str(h): _agg(cby_h[h], with_dm=h) for h in sorted(cby_h)},
            "pit_histogram_overall": pit_histogram(cal_recs),
        }

    if a.emit_calibration:
        cal = emit_calibration(recs, a.cal_warmup)
        with open("calibration.json", "w", encoding="utf-8") as f:
            json.dump(cal, f, ensure_ascii=False, indent=2)
        results["calibration_emitted"] = cal
        print("[생성] calibration.json (production 이 읽을 파일)")

    # ── 단계 D: 앙상블 (calibration.json 밴드 위에서) ──
    ens_cmp = None
    if a.ensemble or a.emit_ensemble:
        _cal = load_calibration_local()          # {h: (z_lo, z_hi)} or {}
        base_c = calibrate_rolling(recs, a.cal_warmup, "model") if _cal else recs
        variants = {
            "baseline(+C)": base_c,
            "ens_equal": ensemble_equal(recs, _cal),
            "ens_invmae": ensemble_invmae(recs, _cal, a.ens_shrink, a.ens_warmup),
        }
        ens_cmp = {}
        for name, rr in variants.items():
            bh = {}
            for r in rr:
                bh.setdefault(r["horizon"], []).append(r)
            ens_cmp[name] = {
                "overall": _agg(rr, with_dm=cfg.max_h),
                "by_horizon": {str(h): _agg(bh[h]) for h in sorted(bh)},
            }
        # 멤버 단독 MAE% (어떤 멤버가 쓸모있나 — 음성결과의 근거)
        member_mae = {}
        for n in _MEMBERS:
            allh, byh = [], {}
            for r in recs:
                e = _member_abs_err(r, n)
                if e is not None:
                    allh.append(e)
                    byh.setdefault(r["horizon"], []).append(e)
            member_mae[n] = {
                "all": _mean(allh),
                "by_h": {str(h): _mean(v) for h, v in sorted(byh.items())},
            }
        results["ensemble_run"] = {
            "params": {"members": list(_MEMBERS), "shrink": a.ens_shrink,
                       "warmup": a.ens_warmup, "mr_win": a.mr_win,
                       "mom_win": a.mom_win, "mom_damping": a.mom_damping,
                       "on_calibration": bool(_cal)},
            "member_mae_pct": member_mae,
            "variants": ens_cmp,
            "verdict": ("음성. naive(랜덤워크)가 전 horizon 최저 MAE. 앙상블(equal/invmae "
                        "모두)이 baseline·naive 보다 MAE·pinball 악화 → production 미적용. "
                        "전망의 값어치는 AI/뉴스 레이어 + 단계 C 밴드에 있음(통계 중심선 아님)."),
        }

    if a.emit_ensemble:
        ens = emit_ensemble(recs, a.ens_shrink, a.ens_warmup)
        with open("ensemble.json", "w", encoding="utf-8") as f:
            json.dump(ens, f, ensure_ascii=False, indent=2)
        results["ensemble_emitted"] = ens
        print("[생성] ensemble.json (production 가중치)")

    # ── 단계 F: sqrt vs garch 변동성 term-structure (항상 둘 다 새로 돌림) ──
    garch_cmp = None
    if a.compare_garch:
        scfg, gcfg = Cfg(a), Cfg(a)
        scfg.vol_model, gcfg.vol_model = "sqrt", "garch"
        srecs = recs if cfg.vol_model == "sqrt" else run(monthly, scfg)
        grecs = recs if cfg.vol_model == "garch" else run(monthly, gcfg)
        _cal_w = a.cal_warmup
        rows_by = {
            "sqrt": srecs,
            "sqrt +C": calibrate_rolling(srecs, _cal_w, "model"),
            "garch": grecs,
            "garch +C": calibrate_rolling(grecs, _cal_w, "model"),
        }
        garch_cmp = {}
        for name, rr in rows_by.items():
            bh = {}
            for r in rr:
                bh.setdefault(r["horizon"], []).append(r)
            garch_cmp[name] = {
                "overall": _agg(rr, with_dm=cfg.max_h),
                "by_horizon": {str(h): _agg(bh[h]) for h in sorted(bh)},
            }
        results["garch_run"] = garch_cmp

    # ── 단계 E: garch vs factor (공통 변동성 팩터) ──
    factor_cmp = None
    if a.compare_factor:
        gcfg, fcfg = Cfg(a), Cfg(a)
        gcfg.vol_model, fcfg.vol_model = "garch", "factor"
        pnl = _build_panel(monthly)
        grecs2 = recs if cfg.vol_model == "garch" else run(monthly, gcfg)
        frecs = recs if cfg.vol_model == "factor" else run(monthly, fcfg, pnl)
        _w = a.cal_warmup
        rows_by = {
            "garch": grecs2,
            "garch +C": calibrate_rolling(grecs2, _w, "model"),
            "factor": frecs,
            "factor +C": calibrate_rolling(frecs, _w, "model"),
        }
        factor_cmp = {}
        for name, rr in rows_by.items():
            bh = {}
            for r in rr:
                bh.setdefault(r["horizon"], []).append(r)
            factor_cmp[name] = {
                "overall": _agg(rr, with_dm=cfg.max_h),
                "by_horizon": {str(h): _agg(bh[h]) for h in sorted(bh)},
            }
        results["factor_run"] = factor_cmp

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(f"{OUT_DIR}/results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    if not a.no_csv:
        cols = [c for c in recs[0].keys() if not c.startswith("_")]
        with open(f"{OUT_DIR}/records.csv", "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(recs)

    def _s(v, w=0):  # None 안전 표시
        return (f"{'-':>{w}}" if w else "-") if v is None else (f"{v:>{w}}" if w else f"{v}")

    def _line(tag, o):
        print(f"  [{tag}] pinball%={_s(o['pinball_pct'])}  MAE%={_s(o['mae_pct'])}  "
              f"bias%={_s(o['bias_pct'])}  cov80={_s(o['cov80'])}  cov50={_s(o['cov50'])}  "
              f"skill={_s(o['skill_vs_naive'])}  PIT_D={_s(o['pit_ks_D'])}")

    o = results["overall"]
    print(f"[백테스트] {len(monthly)}품목 · {len(recs)}건 채점")
    _line("기준", o)
    if cal_recs is not None:
        co = results["calibration_run"]["overall"]
        _line("보정", co)
        dpb = (co["pinball_pct"] or 0) - (o["pinball_pct"] or 0)
        print(f"  → pinball {dpb:+.4f} ({'개선' if dpb < 0 else '악화'}), "
              f"cov80 {o['cov80']}→{co['cov80']} (목표 .80), "
              f"PIT_D {o['pit_ks_D']}→{co['pit_ks_D']}")
    print(f"  DM* vs 나이브={_s(o.get('dm_vs_naive_stat'))} p={_s(o.get('dm_vs_naive_p'))}")
    if ens_cmp is not None:
        print("  ── 단계 D: 앙상블 (C 밴드 위) ──")
        for name, v in ens_cmp.items():
            _line(name, v["overall"])
    if garch_cmp is not None:
        print("  ── 단계 F: 변동성 term-structure ──")
        for name, v in garch_cmp.items():
            _line(name, v["overall"])
    if factor_cmp is not None:
        print("  ── 단계 E: 공통 변동성 팩터 ──")
        for name, v in factor_cmp.items():
            _line(name, v["overall"])
        for h in sorted(by_h):
            gc = factor_cmp["garch +C"]["by_horizon"].get(str(h), {})
            fc = factor_cmp["factor +C"]["by_horizon"].get(str(h), {})
            print(f"     h={h}: cov80 garch+C={_s(gc.get('cov80'))} factor+C={_s(fc.get('cov80'))}"
                  f"  pinball garch+C={_s(gc.get('pinball_pct'))} factor+C={_s(fc.get('pinball_pct'))}")
    print(f"  → {OUT_DIR}/results.json" + ("" if a.no_csv else f" · {OUT_DIR}/records.csv"))
    for h in sorted(by_h):
        ah = results["by_horizon"][str(h)]
        extra = ""
        if cal_recs is not None:
            ch = results["calibration_run"]["by_horizon"].get(str(h), {})
            extra = f"  →보정 MAE%={_s(ch.get('mae_pct'))} cov80={_s(ch.get('cov80'))}"
        if ens_cmp is not None:
            eh = ens_cmp["ens_invmae"]["by_horizon"].get(str(h), {})
            extra += f"  →ens MAE%={_s(eh.get('mae_pct'))}"
        print(f"   h={h}: MAE%={_s(ah['mae_pct'], 7)}  cov80={_s(ah['cov80'], 6)}  "
              f"skill={_s(ah['skill_vs_naive'], 7)}{extra}")


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
# [단계 C — split-conformal 밴드 캘리브레이션 (--calibrate / --emit-calibration)]
#   · 왜: 기본 밴드의 실측 커버리지가 cov80≈0.755(목표 .80)이고 horizon 이
#     길수록 더 좁아진다(h1 .80 → h6 .72). 즉 80% 밴드가 실제로는 72% 만
#     담아 "설득력" 과 tail 경보 기능이 약하다.
#   · 방법: 예측분위 q_p = exp(mu + Qz_h(p)·sigma) 에서 Qz_h(p) 를 정규
#     Φ⁻¹(p) 대신 **표준화 잔차 z=(lnA-mu)/sigma 의 실측 p-분위** 로 교체.
#     이것이 split conformal prediction — 분포가정 없이 커버리지를 구성상
#     목표에 맞춘다. 팩테일이면 80% 밴드만 넓어지고 50% 는 유지되며, 상방/
#     하방 비대칭도 자동 반영된다.
#   · 롤링 OOS 로만 평가: 각 전망월 t 의 캘리브레이션은 t 이전에 이미 실현된
#     잔차(target_month < t)로만 적합 → 캘리브레이션 절차 자체에 look-ahead 없음.
#     워밍업(잔차 40개) 전에는 항등.
#   · center=model(채택): Qz 를 중앙값으로 recenter 해 **스프레드만** 보정,
#     base(점전망)는 모델값 유지. center=conformal(미채택): Qz(0.5) 도 적용 =
#     중앙값 이동. 백테스트상 conformal 은 bias 를 +1.6%p 로 뒤집고 MAE 를
#     11.65→12.65 로 악화 → 편향은 국면 의존적이라 일반화 안 됨(예상대로).
#   · 결과(center=model, 5,622건 롤링 OOS):
#       cov80 0.755→0.789, horizon별 0.78~0.81 로 평탄화(6개월 0.72→0.79),
#       cov50 0.499→0.516, MAE% 불변(중앙값 안 건드림),
#       pinball% 4.038→4.104 (+1.6%).
#   · 게이트 판정: 순수 pinball 은 +1.6% 로 소폭 악화(당초 "무회귀" 게이트
#     탈락). 그러나 pinball 은 대다수(양성) 관측에서 밴드가 좁을수록 유리한
#     지표라, "평균 예리함" 과 "명목 커버리지" 는 근본적으로 상충한다. 이
#     제품(구매·헤지용 bull/bear 시나리오)의 목적함수는 후자 — 6개월 80%
#     밴드가 실제 80% 를 담는 것 — 이고, 그 값이 pinball 1.6% 보다 크다고
#     판단해 **band-only 캘리브레이션을 채택**. (중앙값·pinball 을 직접
#     개선하는 건 단계 D 앙상블의 몫.)
#   · 산출 calibration.json → analyze.py calculate_statistical_bounds 가
#     horizon별 (z_lo, z_hi) 로 읽어 통계 밴드에 적용. 파일 없으면 정규 ±1.2816
#     으로 항등(=기존 동작). 재생성: python backtest.py --emit-calibration.
#   · 한계: horizon별 전역 pooling(품목 무관) — 품목별 잔차 분포 차이는 무시
#     (steel n 부족 때문에 의도적). 상관·군집은 단계 E/F. 잔차 vintage 는
#     현재 CSV 기준.
#
# [단계 D — 앙상블 (--ensemble / --emit-ensemble) → 음성결과, production 미적용]
#   · 멤버: stat(감쇠드리프트) · naive(랜덤워크) · drift(무감쇠) · season(계절) ·
#     meanrev(AR(1) 평균회귀) · mom(OLS 기울기 모멘텀). 앙상블 중심 = 로그공간
#     median 의 가중평균(가중 기하평균). 가중 = 1/MAE (horizon별, equal 로 shrink),
#     롤링 OOS 로만 적합. 밴드는 f_stat sigma + calibration.json.
#   · 멤버 단독 OOS MAE%(전 horizon): naive 11.48 < stat 11.65 < meanrev 11.98
#     < mom 13.26 < drift 13.75 < season 17.01. **랜덤워크가 전 horizon 최저.**
#   · 앙상블 결과(C 밴드 위, 5,622건): equal MAE 12.18 / invmae MAE 12.11 —
#     baseline+C(11.65)·naive(11.48) 보다 **악화**. shrink=0(순수 invmae)도 12.08.
#   · 원인: 최선 모델(naive)에 뭘 섞어도 노이즈만 추가된다. 멤버들이 서로
#     높은 상관이라 분산화 이득이 없고, non-naive 멤버는 (2019~26 추세장에서)
#     체계적 편향을 갖는다. 이는 상품 spot 이 월단위에서 랜덤워크에 가깝다는
#     기존 실증(Meese-Rogoff 류)과 일치.
#   · 결론: 통계 중심선은 현행 유지(damped drift ≈ naive). 전망의 값어치는
#     (a) AI/뉴스 레이어(공급쇼크·정책·재고 — 가격만으론 안 보임)와
#     (b) 단계 C 의 캘리브레이션된 비대칭 밴드에 있다. 앙상블 코드는 분석
#     도구로만 남기고(ensemble.json 미커밋·미적용), 단계 E/F 는 밴드·구조
#     쪽(공통인자·GARCH)에 집중.
#   · 참고로 --drift-damping 0 (= 중심선을 순수 naive 로) 이 현행 0.2 보다
#     OOS MAE 가 0.17%p 낮다. 효과는 작지만 방향은 "드리프트를 더 줄여라".
#
# [단계 F — GARCH(1,1) 변동성 term-structure (--vol-model garch / --compare-garch) → 채택]
#   · 왜: √h 는 "월간 분산이 매달 같다"(독립증분) 가정. 실제 변동성은
#     평균회귀·군집한다. 최근이 조용하면 √h 는 근월을 과대추정, 급등락 직후엔
#     즉각 못 넓힌다. 단계 C 는 이걸 사후 경험분위로 '땜질' 했을 뿐.
#   · 방법: GARCH(1,1) σ²_t = ω + α·ε²_{t-1} + β·σ²_{t-1}. 분산타게팅으로
#     ω = v̄(1-α-β) 고정, (α,β) 는 α∈[.02,.29]·β∈[.60,.93] 조립그리드에서
#     가우시안 로그우도 최대(scipy 불필요). h개월 뒤 로그가격 수준의 분산 =
#     Σ_{k=1..h} E[σ²_{t+k}] = h·v̄ + (σ²_{t+1}-v̄)·(1-(α+β)^h)/(1-(α+β)).
#     α+β<1 이면 h 증가 시 √h 가 아니라 분산이 선형(장기 v̄ 기울기)에 수렴.
#     데이터<24개월·GARCH 실패 시 √h 로 폴백.
#   · 결과(5,622건, C 위):
#       sqrt      pinball 4.038  cov80 .755  skill_vs_naive -0.003
#       sqrt +C   pinball 4.104  cov80 .789  skill -0.019
#       garch     pinball 3.951  cov80 .765  skill +0.019
#       garch +C  pinball 4.007  cov80 .798  skill +0.005   ← 채택
#     garch+C 는 (a) pinball 이 전 horizon sqrt+C 보다 낮고, 원래 baseline
#     (4.038) 도 밑돈다 = 단계 C 의 pinball 비용을 회수, (b) cov80 이 .80 에
#     정확히 도달·평탄, (c) **DM* = -2.554, p = 0.011 로 나이브 대비 스킬이
#     통계적으로 유의**(이 전 과정 통틀어 처음). MAE 는 불변(밴드만 건드림).
#   · 게이트: 통과. 단계 C 와 달리 트레이드오프 없이 개선.
#   · 기전: GARCH 는 "지금" 의 조건부 분산을 쓴다 — 조용한 달엔 근월 밴드가
#     √h·롤링블렌드보다 타이트해 pinball 이 낮고, 변동성 급증 시엔 즉시 넓혀
#     tail 을 잡는다. shape 변화보다 "반응성 있는 σ_1" 의 기여가 크다.
#   · production: analyze.calculate_statistical_bounds 가 36/6m 블렌드 대신
#     _common.garch_sigma_path(월간 로그수익률, months_ahead, floor) 사용.
#     calibration.json 은 반드시 --vol-model garch 로 재생성해야 잔차 기준이
#     맞는다: python backtest.py --vol-model garch --emit-calibration.
#   · 한계: 월 12종 각각 독립 GARCH. 그리드 추정이라 MLE 최적점은 아님
#     (스킬 차이엔 영향 미미). 정규분포 조건부 가정(팩테일은 C 의 conformal 이 흡수).
#
# [단계 E — 공통 변동성 팩터 (--vol-model factor / --compare-factor) → 음성결과, 미적용]
#   · 아이디어: r_i = β_i·F + ε_i. F = 12종 등가중 월간 로그수익률('원자재지수').
#     σ²_{i,h} = β_i²·Var(F_h) + Var(ε_i,h), 각 항 GARCH 누적. 기대효과: 공통
#     국면(2020 코로나·2022 에너지)에 모든 밴드가 함께 팽창, 공통분산은 12종에서
#     추정하니 더 안정.
#   · 결과(5,622건, C 위): factor+C pinball 4.039 (garch+C 4.007 보다 악화),
#     cov80 .801 (garch+C .798 과 사실상 동일), skill_vs_naive -0.003 (garch+C
#     +0.005 → F 가 얻은 유의 스킬을 도로 까먹음), PIT_D 도 소폭 악화.
#   · 게이트: 탈락. 원인: 분산을 β²Var(F)+Var(ε) 로 쪼개고 GARCH 를 2번 + 회귀
#     1번 돌리면서 추정 노이즈가 늘고, 12종·월단위에서 공통팩터의 안정화 이득이
#     그 노이즈를 못 이긴다. per-commodity GARCH(단계 F)가 이미 공통 국면 변화를
#     (자기 시계열에 반영되는 시점에) 충분히 잡고 있음.
#   · 결론: production 미적용. 팩터 코드는 분석 도구로만. 패턴이 뚜렷하다 —
#     밴드를 건드리되 단순한 변경(C 의 conformal, F 의 GARCH)은 통과, 모델
#     복잡도를 키우는 변경(D 앙상블, E 팩터분해)은 탈락.
#   · 남는 E 아이디어(미구현): 계층적(부분 pooling) 캘리브레이션 — C 의 전역
#     pooling 을 품목별 qz 로 shrink. 횡단면 타당성 체크(12종 전망이 지수로
#     환산 시 그럴듯한지) 는 진단용으로만.
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

"""핵심 원자재 AI 가격 전망 생성기.

1. 야후 파이낸스 원자재 8종 + 매크로 4종 시계열 수집, manual/ CSV(니켈·아연·텅스텐) 병합
2. 요약본을 Gemini 에 전달해 6개월 월별 시나리오(스토리·요인지표·과거 유사국면 1년/6개월)를 생성
3. base(중심 전망)는 AI 가 아니라 **통계적으로 계산**(약한 드리프트의 로그수익률 추정)
4. bull/bear 밴드 폭은 **과거 월간 변동성 × √t 스케일링**(랜덤워크 신뢰구간)으로 계산
5. raw_materials_forecast.json 으로 저장 (실패 시 기존 파일 유지)
"""

from __future__ import annotations

import json
import math
import os
import sys
import time

import pandas as pd
from pydantic import BaseModel, Field, create_model

from _common import (
    COMMODITIES, META, build_history, fetch_raw, latest_macro,
    load_manual_history, today_str,
)

API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    sys.exit("[에러] GEMINI_API_KEY 환경변수가 없습니다.")

# 모델: 환경변수(GEMINI_MODEL, 쉼표구분)로 재정의 가능. 앞에서부터 순서대로 시도.
MODELS = [m.strip() for m in os.environ.get("GEMINI_MODEL", "").split(",") if m.strip()] or [
    "gemini-2.5-flash",
    "gemini-flash-lite-latest",
    "gemini-flash-latest",
]

try:
    from google import genai
    from google.genai import types
    _client = genai.Client(api_key=API_KEY)
    _NEW_SDK = True
except ImportError:
    import google.generativeai as _legacy
    _legacy.configure(api_key=API_KEY)
    _NEW_SDK = False

# ==============================================================================
# 1. 데이터 수집 및 COMMODITIES 확정
# ==============================================================================
raw = fetch_raw()
history, spot = build_history(raw)
if not any(history.values()):
    sys.exit("[에러] 원자재 시계열을 하나도 수집하지 못했습니다.")

# 야후에 없는 품목 수동 병합 -> COMMODITIES 리스트에 편입
for _k, _rows in load_manual_history().items():
    history[_k] = _rows
    spot[_k] = _rows[-1]["price"]
    if _k not in COMMODITIES:
        COMMODITIES.append(_k)

try:
    _prev = json.load(open("raw_materials_forecast.json", encoding="utf-8")).get("history_3y", {})
    for _k in COMMODITIES:
        if not history.get(_k) and _prev.get(_k):
            history[_k] = _prev[_k]
            spot[_k] = _prev[_k][-1]["price"]
            print(f"[경고] {_k}: 이번 수집 0행 → 직전 파일값 {len(_prev[_k])}행 보존")
except Exception:  # noqa: BLE001
    pass


# ==============================================================================
# 2. Pydantic 스키마 정의 (Structured Outputs 용)
# ==============================================================================
class MonthlyForecastBase(BaseModel):
    month: str = Field(description="예: '2026-09'")
    price: float = Field(description="이후 파이썬이 재계산하므로 임의의 값(예: 0.0)을 넣어도 무방함")
    rationale: str = Field(description="이 달 가격이 통계적 기준선(conservative_base) 수준일 것으로 보는 근거 한 문장")

class MonthlyForecastBias(BaseModel):
    month: str = Field(description="예: '2026-09'")
    bias: str = Field(description="base 대비 상방/하방 편차 (예: '+8.5%', '-6.2%'). 이 비대칭성으로 AI의 방향성 확신도를 표현.")
    rationale: str = Field(description="해당 월 base 대비 상방/하방으로 벗어날 근거(해당 요인 중심) 한 문장")

class Metric(BaseModel):
    label: str = Field(description="지표명 (예: '위안화 환율')")
    val: str = Field(description="최신 추정치와 단위 (예: '6.7222 (USD/CNY)')")
    date: str = Field(description="기준일 (예: '2026.08.27')")
    cat: str = Field(description="'공급', '수요', '투자', '매크로' 중 하나")
    status: str = Field(description="'강세', '보통', '약세' 중 하나")
    badge: str = Field(description="'danger', 'warning', 'success', 'secondary' 중 하나")

class Analog(BaseModel):
    period: str = Field(description="제공된 값 그대로 복사")
    similarity: str = Field(description="제공된 값 그대로 복사")
    actual: str = Field(description="제공된 값 그대로 복사")
    miniHist: list[float] = Field(description="제공된 배열 그대로 복사")
    miniForecast: list[float] = Field(description="제공된 배열 그대로 복사")
    title: str = Field(description="그 시기에 실제 있었던 역사적 사건명")
    summary: str = Field(description="그 국면의 수급/매크로 배경 요약")

class CommodityForecast(BaseModel):
    name: str = Field(description="원자재 이름 (예: 'WTI 원유 (CME)')")
    unit: str = Field(description="단위 (예: 'USD/bbl')")
    current_price: float = Field(description="현재 실적가")
    forecast_6m_target: float = Field(description="임의의 숫자 (파이썬이 재계산함)")
    forecast_change_rate: str = Field(description="예: '+0.0%' (파이썬이 재계산함)")
    volatility_score: int = Field(description="예상되는 변동성 점수 (1~10)")
    planning_advisor: str = Field(description="초음파 진단기기 부품 조달/구매 담당자를 위한 핵심 1문장 전략 코멘트")
    advisor: str = Field(
        description="최근 시황(단가추이 및 정세) -> 원자재 주요 뉴스(매크로/마이크로) -> 구매 담당자 대응 조언(완제품 자재 가격 전가 및 리스크 헤징) 순서로 작성된 3~4문장"
    )
    monthly_forecast_base: list[MonthlyForecastBase]
    monthly_forecast_bull: list[MonthlyForecastBias]
    monthly_forecast_bear: list[MonthlyForecastBias]
    rationale_base: str = Field(description="기본(통계적 기준선) 시나리오 요약")
    rationale_bull: str = Field(description="낙관 시나리오 요약")
    rationale_bear: str = Field(description="비관 시나리오 요약")
    metrics: list[Metric] = Field(description="가격에 영향이 큰 지표 위주 6~8개")
    analogs: list[Analog]
    analogs_6m: list[Analog]

# 💡 [핵심 해결책] dict(additionalProperties 허용 불가) 대신, 수집이 확정된 
# COMMODITIES 의 키들(wti, copper 등)을 고정 필드로 갖는 Pydantic 모델을 동적 생성
commodity_fields = {k: (CommodityForecast, ...) for k in COMMODITIES}
CommoditiesModel = create_model('CommoditiesModel', **commodity_fields)

class ForecastResponse(BaseModel):
    update_date: str = Field(description="생성 기준일")
    commodities: CommoditiesModel


# ==============================================================================
# 3. 통계적 기준선 및 과거 유사 궤적 연산
# ==============================================================================
history_brief = {}
for k, rows in history.items():
    step = max(1, len(rows) // 70)
    history_brief[k] = rows[::step]

_FWD = 6
_MAX_ANALOGS = 6

def _monthly_series(key: str):
    s = raw.get(key)
    if s is not None and not s.empty:
        return s.resample("ME").last().dropna(), META[key][2]
    rows = history.get(key) or []
    if len(rows) < 24:
        return None, 1.0
    ser = pd.Series(
        {pd.Timestamp(r["date"]): float(r["price"]) for r in rows if r.get("price")}
    ).sort_index()
    return ser.resample("ME").last().dropna(), 1.0

_SIM_MIN = 0.5

def top_analogs(key: str, win: int = 12) -> list[dict]:
    m, mult = _monthly_series(key)
    if m is None or len(m) < win + _FWD + win:
        return []
    excl = max(4, win * 2 // 3)
    cur = m.iloc[-win:]
    cur_path = (cur / cur.iloc[0] * 100.0).to_numpy()
    cur_amp = float(cur_path.max() - cur_path.min())
    cands = []
    for i in range(len(m) - win - _FWD):
        w = m.iloc[i:i + win]
        if w.index[-1] >= m.index[-win]:
            break
        if w.isna().any() or not w.iloc[0]:
            continue
        wp = (w / w.iloc[0] * 100.0).to_numpy()
        if len(wp) != len(cur_path):
            continue
        pc = float(pd.Series(wp).corr(pd.Series(cur_path)))
        if pc != pc:
            continue
        amp = float(wp.max() - wp.min())
        amp_ratio = min(amp, cur_amp) / max(amp, cur_amp) if max(amp, cur_amp) else 0.0
        sim = max(0.0, pc) * (0.6 + 0.4 * amp_ratio)
        if sim >= _SIM_MIN:
            cands.append((sim, i, w, m.iloc[i + win:i + win + _FWD]))

    cands.sort(key=lambda x: -x[0])
    out, used = [], []
    for sim, i, w, after in cands:
        if any(abs(i - j) < excl for j in used):
            continue
        used.append(i)
        hist_p = [round(float(v) * mult, 2) for v in w.values]
        fore_p = [round(float(v) * mult, 2) for v in after.values]
        chg = (fore_p[-1] / hist_p[-1] - 1) * 100 if hist_p and hist_p[-1] else 0.0
        out.append({
            "period": f"'{w.index[0].strftime('%y.%m')}~'{w.index[-1].strftime('%y.%m')}",
            "similarity": f"{sim * 100:.0f}%",
            "actual": f"{chg:+.1f}%",
            "miniHist": hist_p,
            "miniForecast": fore_p,
        })
        if len(out) >= _MAX_ANALOGS:
            break
    return out

analogs_real = {k: top_analogs(k, 12) for k in COMMODITIES}
analogs_real_6m = {k: top_analogs(k, 6) for k in COMMODITIES}

_DRIFT_DAMPING = 0.2
_DRIFT_WINDOW = 12
_VOL_WINDOW = 36
_BAND_Z = 1.28
_AI_TILT_WEIGHT = 0.5

def _monthly_log_returns(key: str, window: int) -> list[float]:
    m, _ = _monthly_series(key)
    if m is None or len(m) < 3:
        return []
    tail = m.iloc[-(window + 1):] if len(m) > window else m
    vals = tail.to_numpy()
    rets = []
    for a, b in zip(vals[:-1], vals[1:]):
        if a and b and a > 0 and b > 0:
            rets.append(math.log(b / a))
    return rets

def conservative_forecast(key: str, cur: float, n: int = 6) -> dict:
    drift_rets = _monthly_log_returns(key, _DRIFT_WINDOW)
    vol_rets = _monthly_log_returns(key, _VOL_WINDOW)
    drift = (sum(drift_rets) / len(drift_rets) * _DRIFT_DAMPING) if drift_rets else 0.0

    if len(vol_rets) >= 3:
        mean_r = sum(vol_rets) / len(vol_rets)
        var = sum((r - mean_r) ** 2 for r in vol_rets) / (len(vol_rets) - 1)
        monthly_vol = math.sqrt(var)
    else:
        monthly_vol = 0.08

    base, vol_up, vol_dn = [], [], []
    for i in range(1, n + 1):
        base.append(round(cur * math.exp(drift * i), 2))
        band = _BAND_Z * monthly_vol * math.sqrt(i)
        vol_up.append(band)
        vol_dn.append(band)
    return {"base": base, "vol_up": vol_up, "vol_dn": vol_dn, "monthly_vol": round(monthly_vol, 4),
            "monthly_drift": round(drift, 4)}

conservative = {k: conservative_forecast(k, spot.get(k) or 1.0) for k in COMMODITIES}
update_date = today_str()
macro = latest_macro(raw)

market_input = {
    "update_date": update_date,
    "macro": macro,
    "current_spot": {k: spot.get(k) for k in COMMODITIES},
    "history_summary": history_brief,
    "conservative_base": {k: conservative[k]["base"] for k in COMMODITIES},
}

FACTORS = {
    "wti": "매크로: 달러(DXY), PMI, 연준 금리, GDP. 마이크로: 원유재고, SPR, OPEC+ 감산량, 셰일 리그, 정제마진, 지정학",
    "copper": "매크로: 중국 부동산/PMI, 달러. 마이크로: LME/SHFE 재고, 제련 TC/RC, 칠레/페루 생산차질, 전력망/AI 데이터센터 수요",
    "aluminum": "매크로: 에너지(가스/석탄) 가격, 중국 탄소중립. 마이크로: 재고, 제련소 감산, 보크사이트(기니) 공급, 경량화/건설 수요",
    "gold": "매크로: 실질금리, 달러, 기대인플레. 마이크로: 중앙은행 매입, ETF 보유량, 지정학 위험지수",
    "silver": "매크로: 금 비율, 실질금리. 마이크로: 태양광 설치, 전장/전자 수요, 부산물 공급 의존, ETF",
    "platinum": "매크로: 디젤차 판매, EV 전환속도. 마이크로: 남아공 전력/파업, 자동차 촉매 로딩량, 팔라듐 스프레드",
    "steel": "매크로: 중국 인프라, 반덤핑 관세. 마이크로: 철광석/원료탄 가격, 조강생산 통제, 자동차/조선 수요, HRC 스프레드",
    "ironore": "매크로: 중국 GDP/부동산, 해상운임(BDI). 마이크로: 호주/브라질 출하량 및 기후, 중국 항구 재고, 고로 가동률",
    "nickel": "매크로: 스테인리스/EV 배터리 수요. 마이크로: 인니 광석/수출정책, 재고, class1/2 스프레드",
    "zinc": "매크로: 건설 인프라 수요. 마이크로: 광산 정광 공급(TC), 재련소 감산, 도금강판 수요",
    "tungsten": "매크로: 절삭공구(제조업 CAPEX), 방산. 마이크로: 중국 수출쿼터/통제, APT 고시가, 서방 공급망 다변화",
}


# ==============================================================================
# 4. 프롬프트 정의
# ==============================================================================
_KEYS_STR = ", ".join(COMMODITIES)

prompt = f"""당신은 글로벌 원자재/거시경제 퀀트 애널리스트입니다.
아래 시장 입력 데이터를 바탕으로 원자재 {len(COMMODITIES)}종({_KEYS_STR})의 6개월 가격 전망 데이터셋을 작성하세요.

**중요 — 가격 숫자는 당신이 만드는 게 아니라 이후 파이썬이 통계적으로 계산합니다.**
market_input.conservative_base 는 "AI 개입 없는 순수 통계 중심선"(참고용)이고,
실제 monthly_forecast_base/bull/bear 의 price 는 이후 코드가:
 - bull/bear 끝점 = 통계 중심선 ± 과거 변동성 기반 밴드 (당신이 못 바꿈)
 - base = 그 밴드 안에서, 당신이 적을 bull/bear 의 bias(상대적 방향성 강도)만큼 가중 이동한 위치
로 재계산합니다. 즉 **당신의 역할은 숫자가 아니라 "방향성 판단(bias)"과 그 근거(rationale) 서술**입니다.

[분석 및 서술 규칙]
1. monthly_forecast_* 는 {update_date} 기준 이후 6개 월 (예: 2026-09 ~ 2027-02).
2. monthly_forecast_bull/bear 에는 price 대신 base 대비 편차(bias, 예: "+8.5%", "-6.2%")를 제시하세요.
   이 bias 의 절대 크기 자체는 밴드폭 계산에 쓰이지 않고, **bull bias 와 bear bias 의 상대적 비율**(어느 쪽 근거가 더 강한지)만 base 위치를 정하는 데 쓰입니다. 우상향 확신이 강하면 bull bias 를 뚜렷하게 크게 적으세요.
3. analogs 와 analogs_6m 은 주어진 리스트의 데이터를 임의 수정 없이 **그대로 복사**하고 title/summary 만 채우세요.
4. metrics 는 제공된 '원자재별 주요 영향 요인'을 참고하여 현재 시점 가장 중요한 변수 6~8개를 선정해 채우세요.

[Advisor (전략 코멘트) 필수 가이드 - 초음파 진단기기 제조사 관점]
최근 시황(단가추이/글로벌 정세) -> 주요 뉴스(매크로/마이크로) -> 구매 담당자 대응 조언 순서로 서술하세요.
아래 품목별 특성을 반드시 반영하여 자재 단가에 적시 반영하거나 리스크 헤징 전략을 제시하세요:
 - WTI: 직접 구매하진 않으나 석유 기반 플라스틱 Cover(레진 소재), 포장재(PE폼), 비닐류 가격에 지대한 영향.
 - 구리: Cable, Heatsink, Bracket 가격에 직접적 영향.
 - 알루미늄: 시스템 Frame, Bracket 등 외장 부품에 대량 사용. 단기 급등 리스크 헷징 필수.
 - 금/은: Connector, FPCB, PCB, Cable 등 핵심 전장부품에 사용됨.
 - 백금: 단결정 생산 설비용으로 직접적 연관은 적으나 추이 모니터링.
 - 열연강판/철광석: 시스템 Frame, Bracket 및 협력사 판금가공품(SPCC 냉연강판)의 기초자재. 통상 1~2개월 후행하여 전가되므로 가격 변곡점 시기 협상 전략 제시 필요.
 - 니켈/아연/텅스텐: 지정학적 요인과 수출 쿼터 등 공급 리스크에 민감하게 대응.

[실제 과거 유사국면 1년/6개월 데이터]
{json.dumps(analogs_real, ensure_ascii=False)}
{json.dumps(analogs_real_6m, ensure_ascii=False)}

[원자재별 주요 영향 요인]
{chr(10).join(f"- {k}: {FACTORS[k]}" for k in COMMODITIES if k in FACTORS)}

[시장 입력 데이터 (conservative_base 는 통계적으로 확정된 값)]
{json.dumps(market_input, ensure_ascii=False)}
"""

# ==============================================================================
# 5. Gemini API 호출
# ==============================================================================
def call_gemini(text: str) -> str:
    last = None
    for model in MODELS:
        for attempt in range(1, 3):
            try:
                print(f"[진행] {model} 호출 (시도 {attempt})…")
                if _NEW_SDK:
                    cfg = types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=ForecastResponse, # 동적으로 생성된 Pydantic 주입
                        temperature=0.35,
                        top_p=0.9,
                        automatic_function_calling={"disable": True},
                    )
                    if "2.5" in model or "gemini-3" in model:
                        cfg.thinking_config = {"thinking_budget": 0}
                    
                    r = _client.models.generate_content(
                        model=model, contents=text, config=cfg,
                    )
                    return r.text
                else:
                    m = _legacy.GenerativeModel(model)
                    return m.generate_content(
                        text,
                        generation_config={
                            "response_mime_type": "application/json",
                            "temperature": 0.35,
                            "top_p": 0.9,
                        },
                    ).text
            except Exception as e:  # noqa: BLE001
                last = e
                wait = 2 ** attempt
                print(f"[경고] {model} 실패: {e} → {wait}s 후 재시도")
                time.sleep(wait)
    raise RuntimeError(f"Gemini 전체 실패: {last}")

def _n(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d

# ==============================================================================
# 6. 통계 혼합 및 결과 후처리 (기존 로직 유지)
# ==============================================================================
def sanitize_scenarios(commodities: dict) -> None:
    for k in COMMODITIES:
        c = commodities[k]
        cons = conservative.get(k) or {}
        base_path = cons.get("base")
        vol_up = cons.get("vol_up")
        vol_dn = cons.get("vol_dn")
        if not base_path:
            continue

        cur = spot.get(k) or _n(c.get("current_price")) or base_path[0]
        c["current_price"] = round(cur, 2)

        base = c.get("monthly_forecast_base") or []
        bull = c.get("monthly_forecast_bull") or []
        bear = c.get("monthly_forecast_bear") or []

        new_base_prices = []
        for i in range(len(base_path)):
            b_stat = base_path[i]
            band = vol_up[i]

            bull_row = bull[i] if i < len(bull) else {}
            bear_row = bear[i] if i < len(bear) else {}
            ai_up_bias = _n(str(bull_row.get("bias", "")).replace("%", ""), None)
            ai_dn_bias = _n(str(bear_row.get("bias", "")).replace("%", ""), None)

            if ai_up_bias is not None and ai_dn_bias is not None:
                au, ad = abs(ai_up_bias), abs(ai_dn_bias)
                total = au + ad
                up_ratio = (au / total) if total > 0 else 0.5
            else:
                up_ratio = 0.5

            bull_price = round(b_stat * math.exp(band), 2)
            bear_price = round(b_stat * math.exp(-band), 2)

            p = 0.5 + _AI_TILT_WEIGHT * (up_ratio - 0.5)
            p = min(max(p, 0.0), 1.0)
            log_base = (1 - p) * math.log(bear_price) + p * math.log(bull_price)
            base_price = round(math.exp(log_base), 2)
            new_base_prices.append(base_price)

            if i < len(bull):
                bull[i]["price"] = bull_price
            else:
                bull.append({**bull_row, "price": bull_price})
            if i < len(bear):
                bear[i]["price"] = bear_price
            else:
                bear.append({**bear_row, "price": bear_price})

            row = base[i] if i < len(base) else {}
            row["price"] = base_price
            if i >= len(base):
                base.append(row)

        c["monthly_forecast_base"] = base[:len(base_path)]
        c["monthly_forecast_bull"] = bull[:len(base_path)]
        c["monthly_forecast_bear"] = bear[:len(base_path)]

        last = new_base_prices[-1]
        c["forecast_6m_target"] = round(last, 2)
        c["forecast_change_rate"] = f"{(last / cur - 1) * 100:+.1f}%"
        c["stat_basis"] = {
            "method": "forecast combination: statistical drift+volatility band weighted by AI bias",
            "monthly_drift": cons.get("monthly_drift"),
            "monthly_vol": cons.get("monthly_vol"),
            "drift_damping": _DRIFT_DAMPING,
            "band_confidence_z": _BAND_Z,
            "ai_tilt_weight": _AI_TILT_WEIGHT,
        }

def fix_months(commodities: dict) -> None:
    y, m, _ = (int(x) for x in update_date.split("-"))
    seq = []
    for _ in range(6):
        m += 1
        if m > 12:
            m, y = 1, y + 1
        seq.append(f"{y:04d}-{m:02d}")

    for k in COMMODITIES:
        c = commodities.get(k) or {}
        for arr in ("monthly_forecast_base", "monthly_forecast_bull", "monthly_forecast_bear"):
            rows = (c.get(arr) or [])[:6]
            for i, r in enumerate(rows):
                r["month"] = seq[i]
            c[arr] = rows

def fix_analogs(commodities: dict) -> None:
    for k in COMMODITIES:
        c = commodities.get(k)
        if not c:
            continue
        for field, real_map in (("analogs", analogs_real), ("analogs_6m", analogs_real_6m)):
            real = real_map.get(k) or []
            got = c.get(field) or []
            merged = []
            for i, rr in enumerate(real):
                g = got[i] if i < len(got) and isinstance(got[i], dict) else {}
                merged.append({
                    **rr,
                    "title": (str(g.get("title") or "").strip() or "과거 유사 구간"),
                    "summary": str(g.get("summary") or "").strip(),
                })
            c[field] = merged

def validate(commodities: dict) -> None:
    need = {"name", "unit", "current_price", "forecast_6m_target",
            "monthly_forecast_base", "rationale_base"}
    missing = [k for k in COMMODITIES if k not in commodities]
    if missing:
        raise ValueError(f"누락된 원자재: {missing}")
    for k in COMMODITIES:
        gaps = need - set(commodities[k])
        if gaps:
            raise ValueError(f"{k} 필드 누락: {sorted(gaps)}")
        if not commodities[k]["monthly_forecast_base"]:
            raise ValueError(f"{k} monthly_forecast_base 비어 있음")

def clamp_vs_previous(commodities: dict, jump: float = 0.22) -> None:
    try:
        prev = json.load(open("raw_materials_forecast.json", encoding="utf-8"))["forecast_data"]
    except Exception:  # noqa: BLE001
        return
    for k in COMMODITIES:
        p, c = prev.get(k), commodities.get(k)
        if not p or not c:
            continue
        for arr in ("monthly_forecast_base", "monthly_forecast_bull", "monthly_forecast_bear"):
            pm = {r.get("month"): _n(r.get("price")) for r in (p.get(arr) or [])}
            for r in c.get(arr) or []:
                pv, nv = pm.get(r.get("month")), _n(r.get("price"))
                if pv and nv and abs(nv / pv - 1) > jump:
                    target = pv * (1 + jump) if nv > pv else pv * (1 - jump)
                    r["price"] = round((nv + target) / 2, 2)


# ==============================================================================
# 7. 실행 및 저장
# ==============================================================================
print("[진행] Gemini 시나리오 서술 생성…")
try:
    parsed_json_str = call_gemini(prompt)
    parsed = json.loads(parsed_json_str)
    
    # 딕셔너리로 변환된 객체를 꺼내어 기존 코드에 주입
    commodities = parsed["commodities"]
    
    validate(commodities)
    fix_months(commodities)        
    fix_analogs(commodities)       
    sanitize_scenarios(commodities) 
    clamp_vs_previous(commodities)   
except Exception as e:  # noqa: BLE001
    print(f"[에러] 전망 생성/검증 실패: {e}. 기존 raw_materials_forecast.json 유지.")
    sys.exit(1)

output = {
    "update_date": update_date,     
    "prices_date": update_date,     
    "macro": macro,
    "history_3y": history,          
    "forecast_data": commodities,
}

with open("raw_materials_forecast.json", "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print("[성공] raw_materials_forecast.json 저장 완료 (Structured Outputs 적용)")

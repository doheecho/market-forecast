"""핵심 원자재 AI 가격 전망 생성기.

1. 야후 파이낸스 원자재 8종 + 매크로 4종 시계열 수집, manual/ CSV(니켈·아연·텅스텐) 병합
2. 요약본을 Gemini 에 전달해 6개월 월별 시나리오(스토리·요인지표·과거 유사국면 1년/6개월)를
   JSON 으로 생성
3. base(중심 전망)는 AI 가 아니라 **통계적으로 계산**(약한 드리프트의 로그수익률 추정) —
   AI 는 그 경로에 대한 rationale(정성 설명)만 채움
4. bull/bear 밴드 폭은 **과거 월간 변동성 × √t 스케일링**(랜덤워크 신뢰구간)으로 계산 —
   AI 가 서술한 상방/하방 비대칭(어느 쪽이 더 벌어지는지)만 반영
5. raw_materials_forecast.json 으로 저장 (실패 시 기존 파일 유지)
"""

from __future__ import annotations

import json
import math
import os
import sys
import time

import pandas as pd

from _common import (
    COMMODITIES, META, build_history, fetch_raw, latest_macro,
    load_manual_history, today_str,
)

API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    sys.exit("[에러] GEMINI_API_KEY 환경변수가 없습니다.")

# 모델: 환경변수(GEMINI_MODEL, 쉼표구분)로 재정의 가능. 앞에서부터 순서대로 시도.
# lite 를 먼저 — 가장 빠르고 thinking 지연이 없다.
MODELS = [m.strip() for m in os.environ.get("GEMINI_MODEL", "").split(",") if m.strip()] or [
    "gemini-flash-lite-latest",
    "gemini-flash-latest",
    "gemini-2.5-flash",
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

raw = fetch_raw()
history, spot = build_history(raw)
if not any(history.values()):
    sys.exit("[에러] 원자재 시계열을 하나도 수집하지 못했습니다.")

# 야후에 없는 품목(니켈·아연·텅스텐)은 manual/<key>.csv 로 주입 → COMMODITIES 에 편입.
for _k, _rows in load_manual_history().items():
    history[_k] = _rows
    spot[_k] = _rows[-1]["price"]
    if _k not in COMMODITIES:
        COMMODITIES.append(_k)

# 이번 수집에서 비어 버린 품목(야후 간헐 실패)은 직전 파일값으로 되살린다.
try:
    _prev = json.load(open("raw_materials_forecast.json", encoding="utf-8")).get("history_3y", {})
    for _k in COMMODITIES:
        if not history.get(_k) and _prev.get(_k):
            history[_k] = _prev[_k]
            spot[_k] = _prev[_k][-1]["price"]
            print(f"[경고] {_k}: 이번 수집 0행 → 직전 파일값 {len(_prev[_k])}행 보존")
except Exception:  # noqa: BLE001
    pass

# AI 프롬프트용 다운샘플 (~70 포인트, 토큰·지연 절약)
history_brief = {}
for k, rows in history.items():
    step = max(1, len(rows) // 70)
    history_brief[k] = rows[::step]

_FWD = 6          # 유사국면 '이후 실제' 궤적 길이(개월)
_MAX_ANALOGS = 6  # 프롬프트·JSON 폭주 방지용 안전 상한


def _monthly_series(key: str):
    """(월말 종가 시리즈, 배수) 반환. 야후 원본이 없으면 history[key] 로 대체."""
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


_SIM_MIN = 0.5  # 유사도(0~1) 채택 문턱. 없으면 0개, 많으면 여러 개


def top_analogs(key: str, win: int = 12) -> list[dict]:
    """현재 최근 `win`개월 '가격 궤적'과 모양·진폭이 닮은 과거 구간을 겹치지 않게
    유사도 내림차순으로 반환. 각 구간의 실제 가격 `win`개 + 이후 6개월 실제 6개.

    유사도 = (정규화 가격경로 상관, 0~1) × (0.6 + 0.4 × 진폭비).
    - 정규화 경로 상관: 첫 값을 100 으로 리베이스한 곡선끼리의 Pearson 상관 →
      '방향·굴곡'이 같은지. (기존의 '월간수익률' 상관은 잔진동 리듬만 봐서,
      +90% 폭등 구간이 -9% 횡보 구간과 90% 유사로 잡히는 오류가 있었음)
    - 진폭비 = min(범위)/max(범위): 한쪽은 급등·한쪽은 횡보처럼 '크기'가 다르면 감점.

    ※ 주의(통계적 한계): 표본이 적은 슬라이딩 윈도우에서 상관 임계값만으로
      구간을 뽑는 방식은 다중비교로 인한 우연한 고상관(데이터 스누핑) 위험이
      있고, 비정상(non-stationary) 가격 레벨의 상관은 추세만으로도 과장될 수
      있다. 여기서 뽑힌 유사국면은 "정량적 예측 근거"가 아니라 AI 서술(analogs
      의 title/summary)을 위한 정성적 참고 자료로만 취급한다.
    """
    m, mult = _monthly_series(key)
    if m is None or len(m) < win + _FWD + win:
        return []

    excl = max(4, win * 2 // 3)  # 겹침 배제 간격
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
        if pc != pc:  # NaN (분산 0 등)
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


analogs_real = {k: top_analogs(k, 12) for k in COMMODITIES}      # 1년 비교
analogs_real_6m = {k: top_analogs(k, 6) for k in COMMODITIES}    # 6개월 비교


# ── 통계적 base 경로 + 변동성 기반 밴드 ──────────────────────────────────
# 기존 방식(AI 가 base 자체를 "그럴듯한 굴곡"으로 생성)은 실제 통계적 근거가
# 약함. 대신:
#   · base  = 현재가에서, 과거 로그수익률의 평균(드리프트)을 "약하게만"
#             반영해 뻗어나가는 경로. 드리프트를 100% 반영하면 최근 추세를
#             그대로 미래로 외삽하는 과신이 되므로 damping(<1)으로 눌러준다.
#             ("금융 시계열은 단기적으로 예측 불가능에 가깝다"는 통념에 맞춘
#             보수적 기준선 — 완전 랜덤워크(드리프트 0)와 추세추종의 중간.)
#   · 밴드  = 과거 월간 로그수익률의 표준편차(σ)를 변동성 척도로 삼아,
#             랜덤워크 가정 하의 표준적 스케일링(√t)으로 t개월 뒤 밴드폭을
#             계산. z 값은 근사 신뢰수준(z=1.28 → 약 80% 구간)이며, 원자재별
#             실제 변동성 차이를 그대로 반영한다(변동성 큰 니켈은 밴드가
#             넓고, 변동성 낮은 금은 좁게 나옴).
_DRIFT_DAMPING = 0.2   # 추세를 20%만 반영 (과신 방지)
_DRIFT_WINDOW = 12     # 드리프트 추정에 쓸 최근 개월 수
_VOL_WINDOW = 36       # 변동성(표준편차) 추정에 쓸 최근 개월 수(짧으면 있는 만큼 사용)
_BAND_Z = 1.28         # 랜덤워크 밴드 신뢰수준 근사치 (약 80%)


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
    """통계적 base 경로 + 변동성 기반 bull/bear 밴드를 계산한다.

    반환: {"base":[...], "vol_up":[...], "vol_dn":[...]}
    - base: 약한 드리프트만 반영한 중심 전망 (완전 flat 은 아니지만 추세를 과신하지 않음)
    - vol_up/vol_dn: 각 월의 밴드 반경(비율). bull = base*(1+vol_up), bear = base*(1-vol_dn)
      의 "출발점"으로 쓰고, AI 가 서술한 비대칭(상방/하방 중 더 벌어지는 쪽)이 있으면
      그 비율만큼 가감한다.
    """
    drift_rets = _monthly_log_returns(key, _DRIFT_WINDOW)
    vol_rets = _monthly_log_returns(key, _VOL_WINDOW)

    drift = (sum(drift_rets) / len(drift_rets) * _DRIFT_DAMPING) if drift_rets else 0.0

    if len(vol_rets) >= 3:
        mean_r = sum(vol_rets) / len(vol_rets)
        var = sum((r - mean_r) ** 2 for r in vol_rets) / (len(vol_rets) - 1)
        monthly_vol = math.sqrt(var)
    else:
        monthly_vol = 0.08  # 히스토리가 너무 짧을 때의 기본값(약 8%/월, 임의 폴백)

    base, vol_up, vol_dn = [], [], []
    for i in range(1, n + 1):
        base.append(round(cur * math.exp(drift * i), 2))
        band = _BAND_Z * monthly_vol * math.sqrt(i)  # √t 스케일링(랜덤워크 신뢰구간)
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
    "current_spot": {k: spot.get(k) for k in COMMODITIES},  # 최근 실적가 — current_price 앵커
    "history_summary": history_brief,
    # AI 에게 "이미 확정된" 통계적 base 경로를 알려주고, 그 경로에 대한 rationale(정성 설명)만
    # 요청한다 — AI 가 숫자 자체를 새로 만들지 않도록 프롬프트에서 명시.
    "conservative_base": {k: conservative[k]["base"] for k in COMMODITIES},
}

# 원자재별 주요 영향 요인 — metrics 선정 가이드로 프롬프트에 주입
FACTORS = {
    "wti": "매크로: 달러인덱스(DXY), 글로벌 제조업 PMI, 美 연준 정책금리·기대인플레(BEI), 美 실질GDP·경기침체 확률, "
           "위안화 환율. "
           "마이크로: EIA 주간 원유재고·쿠싱 재고, SPR(전략비축유) 수준, OPEC+ 실효 감산량·잉여생산능력, "
           "美 셰일 리그카운트·생산량, 정제마진(크랙스프레드), 원유 선물 커브(콘탱고/백워데이션), "
           "관리형자금 순매수 포지션(CFTC), 중동·러시아·베네수엘라 지정학·제재",
    "copper": "매크로: 중국 부동산 착공·PMI·인프라 채권 발행, 글로벌 금리·경기, 달러 방향성. "
              "마이크로: LME/COMEX/SHFE 재고 합계, LME 캔슬드워런트 비율, 제련 TC/RC(가공수수료), "
              "칠레·페루·콩고 광산 생산차질·광석 품위 저하, 정광 수출입, 폐동(스크랩) 공급, "
              "전기차·재생에너지·전력망·AI 데이터센터 실수요, 중국 국가전력망 발주",
    "aluminum": "매크로: 유럽 천연가스·전력 선물, 석탄가, 중국 탄소중립·생산쿼터(4,500만t 캡), 달러·금리. "
                "마이크로: LME 재고·캔슬드워런트, 상하이 재고, 알루미나(원료)·가성소다 가격, 중국·유럽 제련소 감산/재가동, "
                "지역 프리미엄(MJP·유럽 듀티페이드), 보크사이트 공급(기니), 자동차 경량화·건설·포장재 수요",
    "gold": "매크로: 美 10년 실질금리(TIPS, 역상관), 달러인덱스, 기대인플레·CPI, 연준 점도표·금리인하 기대, "
            "글로벌 부채·재정적자·신용리스크. "
            "마이크로: 각국 중앙은행 순매입량(WGC), 금 ETF 보유량 증감, COMEX 투기 순포지션, "
            "실물 프리미엄(상하이 vs 런던), 지정학 위험지수(GPR), 리스크오프 자금흐름",
    "silver": "매크로: 금 가격·Gold-Silver Ratio, 美 실질금리·달러, 글로벌 제조업 PMI. "
              "마이크로: 태양광 설치량·N형 셀 은 사용량, 전장·전자 수요, 산업용 vs 투자용 수요 비중, "
              "광산 공급(구리·아연·연 부산물 의존), 재활용 공급, COMEX 재고·투기 포지션, 은 ETF 자금",
    "platinum": "매크로: 글로벌 경상용차·디젤차 판매, 내연기관→HEV/BEV 전환속도, 달러·금리. "
                "마이크로: 남아공(세계 70%) 전력공급(로드셰딩)·임금협상·파업, 러시아 팔라듐 대체(스위칭) 수요, "
                "자동차 촉매(가솔린 삼원촉매) 로딩량, 수소 연료전지·전해조 수요, WPIC 수급수지, "
                "지상재고(ETF·거래소), 팔라듐-백금 스프레드",
    "steel": "매크로: 중국 부동산·인프라 투자, 글로벌 제조업/건설 PMI, 달러·위안화, 각국 관세·반덤핑(美 232조, EU CBAM). "
             "마이크로: 철광석·원료탄(코킹콜) 가격, 中 조강생산 통제·감산 지침, 중국 철강 수출량·수출증치세 환급, "
             "美 중서부 HRC 스프레드, 전기로 vs 고로 가동률, 자동차·가전·조선 수요, 유통재고·리드타임, "
             "우리회사 관점: 시스템 Frame/Bracket 및 협력사 판금 가공품(SPCC 냉연) 단가에 후행 반영",
    "ironore": "매크로: 중국 GDP·부동산 신규착공·특별채 발행, 달러 방향성, 해상운임(BDI). "
               "마이크로: 호주·브라질(Vale·Rio·BHP·FMG) 출하량·기상(사이클론)·광산사고, 중국 항구 철광석 재고(45개항), "
               "고로 가동률·철강 마진, 스크랩 상대가격, 62%Fe vs 65%/58% 품위 스프레드, 다롄상품거래소 투기 포지션",
    "nickel": "매크로: 스테인리스 수요(중국 300계열), EV 배터리(하이니켈 NCM) 채용률, 달러·금리. "
              "마이크로: 인도네시아 광석·NPI·MHP 증설 및 수출정책(RKAB 쿼터), LME 재고·중국 보세재고, "
              "1급(class1) vs 2급 니켈 스프레드, 청산니켈 전환량, 인니 로열티·수출세",
    "zinc": "매크로: 글로벌 건설·인프라(아연도금 강재) 수요, 달러·중국 경기. "
            "마이크로: 광산 정광 공급(TC 제련수수료 방향), LME/SHFE 재고·캔슬드워런트, 주요 제련소 감산/정비, "
            "중국 자동차·백색가전 도금강판 수요, 다이캐스팅 합금 수요",
    "tungsten": "매크로: 절삭공구·초경합금 수요(글로벌 제조업 CAPEX), 방산·항공우주 수요, 미·중 갈등. "
                "마이크로: 중국(세계 80%+) 채굴·수출 쿼터 및 수출통제, APT(암모늄파라텅스텐) 유럽 고시가, "
                "중국 광산 품위 저하, 스크랩(초경 재생) 회수율, 미국·EU 전략비축·공급망 다변화",
}

SCHEMA_ONE = """{
  "name": "WTI 원유 (CME)", "unit": "USD/bbl",
  "current_price": 0.0, "forecast_6m_target": 0.0, "forecast_change_rate": "+0.0%", "volatility_score": 0,
  "planning_advisor": "구매/헤지 담당자를 위한 한 문장 전략 코멘트",
  "advisor": "원자재 구매 담당자를 위한 3~4문장. 최근 시황 / 관련 글로벌 정세 / 알아야 할 주요 뉴스 / 대응 조언 순서로 서술.",
  "monthly_forecast_base": [ {"month": "2026-09", "price": 0.0, "rationale": "이 달 가격이 통계적 기준선(conservative_base) 수준일 것으로 보는 근거 한 문장. 가격 자체는 이미 주어졌으니 숫자를 새로 만들지 말고 근거만 서술."} ],
  "monthly_forecast_bull": [ {"month": "2026-09", "bias": "+0.0%", "rationale": "해당 월 base 대비 상방으로 벗어날 근거(상방 요인 중심) 한 문장"} ],
  "monthly_forecast_bear": [ {"month": "2026-09", "bias": "-0.0%", "rationale": "해당 월 base 대비 하방으로 벗어날 근거(하방 요인 중심) 한 문장"} ],
  "rationale_base": "기본(통계적 기준선) 시나리오 요약", "rationale_bull": "낙관 요약", "rationale_bear": "비관 요약",
  "metrics": [ {"label": "위안화 환율", "val": "6.7222 (USD/CNY)", "date": "2026.08.27", "cat": "수요", "status": "보통", "badge": "secondary"} ],
  "analogs": [ {"period": "(주어진 값 그대로)", "similarity": "(주어진 값)", "actual": "(주어진 값)",
    "miniHist": "(주어진 배열 그대로)", "miniForecast": "(주어진 배열 그대로)",
    "title": "그 시기에 실제 있었던 역사적 사건명", "summary": "그 국면의 수급/매크로 배경 요약"} ],
  "analogs_6m": [ {"period": "(6개월 리스트의 값 그대로)", "similarity": "(주어진 값)", "actual": "(주어진 값)",
    "miniHist": "(주어진 배열 그대로)", "miniForecast": "(주어진 배열 그대로)",
    "title": "그 시기 실제 사건명", "summary": "그 국면 배경 요약"} ]
}"""

_KEYS_STR = ", ".join(COMMODITIES)
_SCHEMA_KEYS = ", ".join(
    f'"{k}": {SCHEMA_ONE}' if k == COMMODITIES[0] else f'"{k}": {{...}}'
    for k in COMMODITIES
)

prompt = f"""당신은 글로벌 원자재/거시경제 퀀트 애널리스트입니다.

아래 시장 입력 데이터를 바탕으로 원자재 {len(COMMODITIES)}종({_KEYS_STR})의
6개월 가격 전망 데이터셋을 순수 JSON 으로만 작성하세요. 마크다운/설명 금지.

**중요 — base 가격은 이미 통계적으로 계산되어 market_input.conservative_base 에 주어져
있습니다. 당신은 그 숫자를 그대로 monthly_forecast_base[].price 에 복사하고,
"왜 이 수준일 것으로 보는지"에 대한 rationale(정성 설명, 한 문장)만 채우세요.
base 가격 자체를 새로 만들거나 바꾸지 마세요.**

규칙:
- monthly_forecast_* 는 {update_date} 기준 이후 6개 월 (예: 2026-09 ~ 2027-02).
- monthly_forecast_base 의 price 는 conservative_base 값을 그대로 사용, rationale 만 작성.
- monthly_forecast_bull/bear 는 price 대신 base 대비 편차(bias, 예: "+8.5%", "-6.2%")만
  제시하세요. 실제 폭(밴드)은 이후 파이썬이 과거 변동성 기반으로 재계산하며, 당신이 준
  bias 의 "방향성과 상대적 크기"(어느 달에 더 벌어지는지, 상방/하방 중 어느 쪽이 더 큰지)만
  참고합니다. 즉 bias 의 절대값보다 "이번 달이 저번달보다 더 벌어지는가", "이번 이벤트로
  상방이 하방보다 더 큰가" 같은 상대적 패턴이 중요합니다.
- 알려진 이벤트·계절성(OPEC+ 회의, 재고 사이클, 중국 정책 시점, FOMC 등)을 rationale/bias 에 반영.
- 6개월 누적 변화폭(conservative_base 기준)은 이미 보수적으로 계산되어 있으므로 그대로 존중.
- badge 는 danger/warning/success/secondary 중 하나. cat 은 공급/수요/투자/매크로 중 하나.
- metrics 는 각 원자재의 아래 '주요 영향 요인' 중 현시점에서 가격에 영향이 큰 것 위주로
  6~8개 선정하고(매크로·마이크로 균형있게), label 에 지표명, val 에 최신 추정치와 단위,
  cat(공급/수요/투자/매크로)·status(강세/보통/약세)·badge 를 채우세요.
- analogs 는 아래 '실제 과거 유사국면(1년)', analogs_6m 은 '실제 과거 유사국면(6개월)' 리스트의
  각 항목당 1개씩 만드세요(리스트 순서·개수 그대로. 리스트가 비어 있으면 빈 배열).
  이 유사국면은 정량적 근거가 아니라 참고 서술용입니다.
  period/similarity/actual/miniHist/miniForecast 는 주어진 값을 **그대로 복사**(임의 생성 금지),
  title(그 시기 실제 사건명)·summary 만 각 항목에 맞게 채우세요.
- advisor 는 최근 시황 → 글로벌 정세 → 주요 뉴스 → 구매 담당자 대응 조언 순의 3~4문장.
  최근 시황은 이 원자재의 최근 단가변동과 단가추이를 보여주면서최근의 글로벌 정세에 대해서 함께 설명해주는게 좋을것같아.
  전반적으로 증권사들 보고하는 형태로 풀어주고, 그 이후에 원자재의 연관된 주요 뉴스들을 매크로/마이크로시점에서 각각 풀어써줘
  그 뒤에는 구매 담당자에게 조언하는 형태로 마무리해주면 될것같다.
  우리회사는 초음파 진단기기를 만드는 회사고, 구매담당자들은 그 제품을 구성하는 원자재를 구매하고있음
  직접 구매하거나, 우리 협력사가 구매하는 자재에 해당 원자재들이 하위 n차 단계에서 사용되니 그 영향을 미리 전망하고
  원자재가 변동을 자재 단가에 적시 반영하는것이 중요함. 가격이 오를 전망이면 우리회사에 미칠 영향을 미리 전망/Risk 헷징 전략세우고
  가격이 떨어질 전망이면 떨어지는 시점에 완제품 자재 가격에 반영되는 원자재가격을 적시 반영하는 것이 중요함
  - WTI의 경우 석유를 우리가 직접 사진 않지만, 석유로 만들어지는 플라스틱 Cover(레진 소재), 포장재(PE폼), 비닐류의 영향이 큼
  - 구리의 경우 Cable, Heatsink, Bracket 가격에 영향
  - 알루미늄은 주로 시스템의 Frame, Bracket 등 외장부품에 많이 사용됨
  - 금은 Connector와 FPCB, PCB, Cable 등 다양한 곳에 사용되고 있고, 은도 일부 Connector에 사용됨
  - 백금은 단결정의 생산 설비에 사용되므로 우리에게 직접 영향이 있지는 않음
  - 열연강판,철광석은 시스템 Frame·Bracket, 협력사 판금 가공품(SPCC 냉연강판)의 상위 원자재로, 방향성 참고용. 열연 HRC → 냉연 SPCC 로 통상 1~2개월 후행 전가됨
- 단위: wti USD/bbl, copper·aluminum USD/ton, gold·platinum·silver USD/oz.t,
  steel USD/s.ton, ironore USD/dmt.

[실제 과거 유사국면 (1년) — 실거래 가격궤적 유사도 분석]
{json.dumps(analogs_real, ensure_ascii=False)}

[실제 과거 유사국면 (6개월) — 실거래 가격궤적 유사도 분석]
{json.dumps(analogs_real_6m, ensure_ascii=False)}

[원자재별 주요 영향 요인]
{chr(10).join(f"- {k}: {FACTORS[k]}" for k in COMMODITIES if k in FACTORS)}

[시장 입력 데이터 (conservative_base 는 통계적으로 이미 확정된 값)]
{json.dumps(market_input, ensure_ascii=False)}

응답 스키마 (commodities 의 각 값은 아래 형태, name/unit 은 원자재에 맞게):
{{ "update_date": "{update_date}", "commodities": {{ {_SCHEMA_KEYS} }} }}
"""


def call_gemini(text: str) -> str:
    last = None
    for model in MODELS:
        for attempt in range(1, 3):
            try:
                print(f"[진행] {model} 호출 (시도 {attempt})…")
                if _NEW_SDK:
                    cfg = {
                        "response_mime_type": "application/json",
                        "automatic_function_calling": {"disable": True},  # AFC 경고 억제
                        "temperature": 0.35,
                        "top_p": 0.9,
                    }
                    if "2.5" in model or "gemini-3" in model:
                        cfg["thinking_config"] = {"thinking_budget": 0}  # 사고 지연 제거
                    r = _client.models.generate_content(
                        model=model, contents=text,
                        config=types.GenerateContentConfig(**cfg),
                    )
                    return r.text
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


def sanitize_scenarios(commodities: dict) -> None:
    """base 는 통계적으로 확정된 conservative_base 값으로 강제 치환하고,
    bull/bear 는 AI 가 준 bias(상대적 비대칭)를 참고해 과거 변동성 기반 밴드로 재계산한다.

    - base: conservative[k]["base"] 로 완전히 덮어씀 (AI 숫자 무시, rationale 텍스트만 채택)
    - bull/bear 밴드: vol_up/vol_dn(√t 스케일링된 변동성 밴드)을 기본으로 하되,
      AI 가 bias 로 표현한 상방/하방 비대칭 비율이 있으면 그 비율만큼 밴드를 상방/하방으로
      기울여 반영한다(밴드의 '전체 폭'은 통계량이 결정, '기울기'만 AI 서술 반영).
    - forecast_6m_target / change_rate 재계산
    """
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

        # base 가격을 통계적 경로로 강제 치환 (rationale 텍스트는 AI 것 유지)
        for i in range(len(base_path)):
            row = base[i] if i < len(base) else {}
            row["price"] = base_path[i]
            if i >= len(base):
                base.append(row)
        c["monthly_forecast_base"] = base[:len(base_path)]

        # bull/bear: 통계적 밴드(vol_up/vol_dn)를 기본 폭으로, AI 의 bias 비율로
        # 상/하 비대칭만 반영 (밴드 절대 폭 자체를 AI 가 부풀리지 못하게 함)
        for i in range(len(base_path)):
            b = base_path[i]
            up_band = vol_up[i]
            dn_band = vol_dn[i]

            bull_row = bull[i] if i < len(bull) else {}
            bear_row = bear[i] if i < len(bear) else {}
            ai_up_bias = _n(str(bull_row.get("bias", "")).replace("%", ""), None)
            ai_dn_bias = _n(str(bear_row.get("bias", "")).replace("%", ""), None)

            # AI 가 상/하 비대칭을 시사했으면(둘 다 있을 때) 그 비율로 밴드를 기울임.
            # 예: ai_up=10, ai_dn=4 면 상방이 하방의 2.5배 -> 통계적 총 밴드폭을
            # 유지한 채 상/하로 나눠 재배분. 정보가 없으면 대칭(50:50).
            if ai_up_bias is not None and ai_dn_bias is not None:
                au, ad = abs(ai_up_bias), abs(ai_dn_bias)
                total = au + ad
                if total > 0:
                    up_ratio = au / total
                else:
                    up_ratio = 0.5
            else:
                up_ratio = 0.5

            total_band = up_band + dn_band
            up_band = total_band * up_ratio
            dn_band = total_band * (1 - up_ratio)

            bull_price = round(b * math.exp(up_band), 2)
            bear_price = round(b * math.exp(-dn_band), 2)

            if i < len(bull):
                bull[i]["price"] = bull_price
            else:
                bull.append({**bull_row, "price": bull_price})
            if i < len(bear):
                bear[i]["price"] = bear_price
            else:
                bear.append({**bear_row, "price": bear_price})

        c["monthly_forecast_bull"] = bull[:len(base_path)]
        c["monthly_forecast_bear"] = bear[:len(base_path)]

        last = base_path[-1]
        c["forecast_6m_target"] = round(last, 2)
        c["forecast_change_rate"] = f"{(last / cur - 1) * 100:+.1f}%"
        # 근거 화면 하단 표기용 — 이번에 계산에 쓴 통계량을 그대로 노출
        c["stat_basis"] = {
            "method": "conservative log-drift base + sqrt(t) volatility band",
            "monthly_drift": cons.get("monthly_drift"),
            "monthly_vol": cons.get("monthly_vol"),
            "drift_damping": _DRIFT_DAMPING,
            "band_confidence_z": _BAND_Z,
        }


def fix_months(commodities: dict) -> None:
    """AI 가 월 라벨을 어긋나게(순서 뒤섞임·과거월) 내는 경우가 있어,
    update_date 다음 달부터 연속 6개월로 강제하고 배열은 6개로 자른다."""
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
    """유사국면의 수치(period·similarity·actual·miniHist·miniForecast)는 파이썬이 계산한
    실제 값으로 강제하고, AI 에게서는 title·summary 만 취한다(1년·6개월 두 리스트)."""
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
    """실행 간 '급변'만 억제한다. base 는 이미 통계적으로 계산되어 매 실행 급변이
    구조적으로 크지 않지만, 변동성 급등 구간 대비 안전장치로 유지한다."""
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


print("[진행] Gemini 시나리오 서술 생성…")
try:
    parsed = json.loads(call_gemini(prompt))
    commodities = parsed["commodities"]
    validate(commodities)
    fix_months(commodities)        # 월 라벨을 연속 6개월로 강제(순서 꼬임 방지)
    fix_analogs(commodities)       # 유사국면 수치는 실제값 강제, AI 는 title/summary 만
    sanitize_scenarios(commodities)  # base 는 통계값으로 강제, bull/bear 는 변동성 밴드로 재계산
    clamp_vs_previous(commodities)   # 실행 간 급변만 추가 억제
except Exception as e:  # noqa: BLE001
    print(f"[에러] 전망 생성/검증 실패: {e}. 기존 raw_materials_forecast.json 유지.")
    sys.exit(1)

output = {
    "update_date": update_date,     # AI 전망 생성일
    "prices_date": update_date,     # 시세 갱신일 (prices.py 가 매일 덮어씀)
    "macro": macro,
    "history_3y": history,          # (호환) 키 이름 유지
    "forecast_data": commodities,
}

with open("raw_materials_forecast.json", "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print("[성공] raw_materials_forecast.json 저장 완료")

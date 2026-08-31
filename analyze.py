"""핵심 원자재 AI 가격 전망 생성기.

1. 야후 파이낸스 원자재 8종 + 매크로 4종 시계열 수집, manual/ CSV(니켈·아연·텅스텐) 병합
2. 요약본을 Gemini 에 전달해 6개월 월별 전망(base/bull/bear) · 시나리오 · 요인지표 ·
   과거 유사국면(1년/6개월 두 비교창)을 JSON 으로 생성
3. raw_materials_forecast.json 으로 저장 (실패 시 기존 파일 유지)
"""
from __future__ import annotations

import json
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
    """
    m, mult = _monthly_series(key)
    if m is None or len(m) < win + _FWD + win:
        return []
    excl = max(4, win * 2 // 3)          # 겹침 배제 간격
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
        if pc != pc:                     # NaN (분산 0 등)
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
analogs_real_6m = {k: top_analogs(k, 6) for k in COMMODITIES}     # 6개월 비교


update_date = today_str()
macro = latest_macro(raw)
market_input = {
    "update_date": update_date,
    "macro": macro,
    "current_spot": {k: spot.get(k) for k in COMMODITIES},  # 최근 실적가 — current_price 앵커
    "history_summary": history_brief,
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
  "monthly_forecast_base": [ {"month": "2026-09", "price": 0.0, "rationale": "해당 월 기본 시나리오 가격 근거 한 문장"} ],
  "monthly_forecast_bull": [ {"month": "2026-09", "price": 0.0, "rationale": "해당 월 낙관 시나리오 가격 근거(상방 요인 중심) 한 문장"} ],
  "monthly_forecast_bear": [ {"month": "2026-09", "price": 0.0, "rationale": "해당 월 비관 시나리오 가격 근거(하방 요인 중심) 한 문장"} ],
  "rationale_base": "기본 시나리오 요약", "rationale_bull": "낙관 요약", "rationale_bear": "비관 요약",
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

규칙:
- monthly_forecast_* 는 {update_date} 기준 이후 6개 월 (예: 2026-09 ~ 2027-02).
- monthly_forecast_base/bull/bear 세 배열 모두 각 월에 price 와 rationale(한 문장)을 넣으세요.
  bull 은 상방 요인, bear 는 하방 요인 중심으로 근거를 서술.
- current_price 는 위 current_spot 값(최근 실적가)과 동일하게, base 첫 달은 거기서 ±4% 이내 출발.
- base 경로는 '직선'이 아니라 실제 전망처럼 **월별로 방향 전환·되돌림·기울기 변화**가
  나타나야 합니다. 참고 근거:
  · 위 '실제 과거 유사국면'의 miniForecast(그 국면 이후 실제 6개월 궤적)의 '모양'을 참고
    (그대로 복사 말고, 현재 매크로/컨센서스에 맞춰 조정).
  · 알려진 이벤트·계절성(OPEC+ 회의, 재고 사이클, 중국 정책 시점, FOMC 등)을 월에 반영.
  · 방향이 바뀔 근거가 있으면 그 달에 고점/저점을 만들어도 됩니다. 단일 방향 6연속 지양.
- 각 월에서 bear.price < base.price < bull.price 는 반드시 유지. bull/bear 는 base 대비
  불확실성으로, 시간이 갈수록 스프레드가 벌어지되 이벤트 리스크가 큰 달은 더 크게.
- 6개월 누적 변화폭은 대체로 ±25% 이내(초강세/초약세 국면이면 근거와 함께 초과 가능).
- badge 는 danger/warning/success/secondary 중 하나. cat 은 공급/수요/투자/매크로 중 하나.
- metrics 는 각 원자재의 아래 '주요 영향 요인' 중 현시점에서 가격에 영향이 큰 것 위주로
  6~8개 선정하고(매크로·마이크로 균형있게), label 에 지표명, val 에 최신 추정치와 단위,
  cat(공급/수요/투자/매크로)·status(강세/보통/약세)·badge 를 채우세요.
- analogs 는 아래 '실제 과거 유사국면(1년)', analogs_6m 은 '실제 과거 유사국면(6개월)' 리스트의
  각 항목당 1개씩 만드세요(리스트 순서·개수 그대로. 리스트가 비어 있으면 빈 배열).
  period/similarity/actual/miniHist/miniForecast 는 주어진 값을 **그대로 복사**(임의 생성 금지),
  title(그 시기 실제 사건명)·summary 만 각 항목에 맞게 채우세요.
- advisor 는 최근 시황 → 글로벌 정세 → 주요 뉴스 → 구매 담당자 대응 조언 순의 3~4문장.
  최근 시황은 이 원자재의 최근 단가변동과 단가추이를 보여주면서최근의 글로벌 정세에 대해서 함께 설명해주는게 좋을것같아.
  전반적으로 증권사들 보고하는 형태로 풀어주고, 그 이후에 원자재의 연관된 주요 뉴스들을 매크로/마이크로시점에서 각각 풀어써줘
  그 뒤에는 구매 담당자에게 조언하는 형태로 마무리해주면 될것같다. 
  우리회사는 초음파 진단기기를 만드는 회사고, 구매담당자들은 그 제품을 구성하는 원자재를 구매하고있음 (이걸 AI Advisor가 따로 언급할 필요는 없음)
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
- 구리, 알루미늄, 금, 은의 경우 케이블, 히트싱크, 커넥터, 프레임 등 협력사 단가에 즉각 반영되는 품목임. 공급망 차질뉴스 (광산 파업, 제련소 이슈)에 매우 민감하게 반응하도록 
  가중치를 더 부요할 필요가 있고, 단기 급등시 선제 구매를 통한 헷징의 필요성이 있음
- 철광석, 열연강판, WTI 등은 프레임, 브라켓 단가로 반영되기까지 2개월 정도의 시차가 있음. 가격 하락기에 진입할 때엔 협력사의 단가 인하 협상을 준비할 타이밍을 구체적으로 짚어주는 것이 중요함
- 텅스텐, 니켈, 아연은 중국의 수출통제나 특정 국가(인도네시아 등)의 정책에 따라 가격이 요동침. 매크로 지표보다 지정학적 리스크와 수출 통제 뉴스가 발생할 경우 Bull Band 상단으로 극단적인 상향조정 하는 규칙이필요함.
- 최근 변동성 가중(EMA)를 반영하여, Bull/Bear 밴드는 최근 3~6개월의변동성에 더 큰 가중치를 두는 지수이동평균을 사용하거나 최근 12개월 변동성을 혼합하여, 최근 원자재가격이
  급등락했을 경우, 다음달의 Bull/Bear밴드가 즉각적으로 넓어져 실제 시장의 불확실성을 더 잘 반영하게 해야

[실제 과거 유사국면 (1년) — 실거래 가격궤적 유사도 분석]
{json.dumps(analogs_real, ensure_ascii=False)}

[실제 과거 유사국면 (6개월) — 실거래 가격궤적 유사도 분석]
{json.dumps(analogs_real_6m, ensure_ascii=False)}

[원자재별 주요 영향 요인]
{chr(10).join(f"- {k}: {FACTORS[k]}" for k in COMMODITIES if k in FACTORS)}

[시장 입력 데이터]
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
                        "temperature": 0.35,  # 월별 변동은 살리되 실행 간 안정은 clamp_vs_previous 로
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
    """AI 의 월별 경로 '모양'은 최대한 살리고, 병리적 케이스만 최소 개입으로 바로잡는다.
    - 현재가는 spot 으로 고정
    - 극단 이상치만 절대 밴드(spot 의 0.5~2.0배)로 클립
    - 각 월에서 bear < base < bull 이 되도록 어긋난 쪽만 살짝 벌림(경로는 안 뭉갬)
    - 월 변동이 ±30% 를 넘는 튐만 30% 로 제한
    - forecast_6m_target / change_rate 재계산"""
    for k in COMMODITIES:
        c = commodities[k]
        base = c.get("monthly_forecast_base") or []
        bull = c.get("monthly_forecast_bull") or []
        bear = c.get("monthly_forecast_bear") or []
        if not base:
            continue

        cur = spot.get(k) or _n(c.get("current_price"))
        if not cur or cur <= 0:
            cur = _n(base[0].get("price"), 1.0)
        c["current_price"] = round(cur, 2)

        lo_abs, hi_abs = cur * 0.5, cur * 2.0

        # 1) base 경로: 튐만 제한, 모양은 유지
        prev = cur
        for row in base:
            v = _n(row.get("price"), prev) or prev
            v = min(max(v, prev * 0.7), prev * 1.3)     # 월 변동 ±30% 초과만 제한
            v = min(max(v, lo_abs), hi_abs)             # 극단 이상치만 클립
            row["price"] = round(v, 2)
            prev = v

        # 2) bull/bear 스프레드: 뒤로 갈수록 '반드시' 벌어지게(단조 증가). 상·하방 비대칭은 유지.
        base_sp, grow = 0.02, 0.024                     # 2% → 6개월차 약 14%
        spu_prev = spd_prev = 0.0
        for i, row in enumerate(base):
            b = row["price"]
            gu = _n((bull[i] or {}).get("price")) if i < len(bull) else None
            gd = _n((bear[i] or {}).get("price")) if i < len(bear) else None
            spu = base_sp + grow * i
            spd = base_sp + grow * i
            if gu and gu > b:                           # AI 가 더 넓게 봤으면 그만큼 반영
                spu = max(spu, gu / b - 1)
            if gd and gd < b:
                spd = max(spd, 1 - gd / b)
            # 매달 최소 1.2%p 는 더 벌어지게(단조 증가 + 실제로 '점점' 넓어지는 팬), 상한 50%
            spu = min(max(spu, spu_prev + 0.012 if i else spu), 0.5)
            spd = min(max(spd, spd_prev + 0.012 if i else spd), 0.5)
            spu_prev, spd_prev = spu, spd
            if i < len(bull):
                bull[i]["price"] = round(min(b * (1 + spu), hi_abs * 1.3), 2)
            if i < len(bear):
                bear[i]["price"] = round(max(b * (1 - spd), lo_abs * 0.7), 2)

        last = base[-1]["price"]
        c["forecast_6m_target"] = round(last, 2)
        c["forecast_change_rate"] = f"{(last / cur - 1) * 100:+.1f}%"


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
    """실행 간 '급변'만 억제한다(완만화 X). 같은 달의 직전 전망 대비
    변화가 jump 를 넘으면 그 절반만 반영해 튐을 눌러준다. AI 경로 모양은 유지."""
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


print("[진행] Gemini 전망 생성…")
try:
    parsed = json.loads(call_gemini(prompt))
    commodities = parsed["commodities"]
    validate(commodities)
    fix_months(commodities)          # 월 라벨을 연속 6개월로 강제(순서 꼬임 방지)
    fix_analogs(commodities)         # 유사국면 수치는 실제값 강제, AI 는 title/summary 만
    clamp_vs_previous(commodities)   # 실행 간 급변만 억제(경로 모양 유지)
    sanitize_scenarios(commodities)  # 이상치·역전만 최소 보정 + target 재계산
except Exception as e:  # noqa: BLE001
    print(f"[에러] 전망 생성/검증 실패: {e}. 기존 raw_materials_forecast.json 유지.")
    sys.exit(1)

output = {
    "update_date": update_date,   # AI 전망 생성일
    "prices_date": update_date,   # 시세 갱신일 (prices.py 가 매일 덮어씀)
    "macro": macro,
    "history_3y": history,        # (호환) 키 이름 유지
    "forecast_data": commodities,
}
with open("raw_materials_forecast.json", "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)
print("[성공] raw_materials_forecast.json 저장 완료")

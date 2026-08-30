"""6대 핵심 원자재 AI 가격 전망 생성기.

1. 야후 파이낸스에서 원자재 6종 + 매크로 4종의 시계열을 수집(최근 3년 일봉 + 그 이전 주봉)
2. 요약본을 Gemini 에 전달해 6개월 월별 전망(base/bull/bear) · 시나리오 · 요인지표 ·
   과거 유사국면을 JSON 으로 생성
3. raw_materials_forecast.json 으로 저장 (실패 시 기존 파일 유지)
"""
from __future__ import annotations

import json
import os
import sys
import time

import pandas as pd

from _common import (
    COMMODITIES, META, build_history, fetch_raw, latest_macro, today_str,
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

# AI 프롬프트용 다운샘플 (~70 포인트, 토큰·지연 절약)
history_brief = {}
for k, rows in history.items():
    step = max(1, len(rows) // 70)
    history_brief[k] = rows[::step]


def top_analogs(key: str, n: int = 2) -> list[dict]:
    """현재 12개월 궤적과 월간수익률 상관이 높은 과거 구간 상위 n개(겹치지 않게)를
    실데이터에서 탐색. 각 구간의 실제 가격 12개 + 이후 6개월 실제 6개를 그대로 반환."""
    s = raw.get(key)
    if s is None or s.empty:
        return []
    m = s.resample("ME").last().dropna()
    if len(m) < 12 + 6 + 12:
        return []
    mult = META[key][2]
    cur_ret = m.iloc[-12:].pct_change().dropna().values

    cands = []
    for i in range(len(m) - 12 - 6):
        win = m.iloc[i:i + 12]
        if win.index[-1] >= m.index[-12]:
            break
        r = win.pct_change().dropna().values
        if len(r) != len(cur_ret):
            continue
        c = float(pd.Series(r).corr(pd.Series(cur_ret)))
        if c == c and c >= 0.3:
            cands.append((c, i, win, m.iloc[i + 12:i + 18]))
    cands.sort(key=lambda x: -x[0])

    out, used = [], []
    for c, i, win, after in cands:
        if any(abs(i - j) < 8 for j in used):   # 8개월 이내 겹침 배제
            continue
        used.append(i)
        hist_p = [round(float(v) * mult, 2) for v in win.values]
        fore_p = [round(float(v) * mult, 2) for v in after.values]
        chg = (fore_p[-1] / hist_p[-1] - 1) * 100 if hist_p and hist_p[-1] else 0.0
        out.append({
            "period": f"'{win.index[0].strftime('%y.%m')}~'{win.index[-1].strftime('%y.%m')}",
            "similarity": f"{max(0.0, c) * 100:.0f}%",
            "actual": f"{chg:+.1f}%",
            "miniHist": hist_p,
            "miniForecast": fore_p,
        })
        if len(out) >= n:
            break
    return out


analogs_real = {k: top_analogs(k, 2) for k in COMMODITIES}


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
    "title": "그 시기에 실제 있었던 역사적 사건명", "summary": "그 국면의 수급/매크로 배경 요약"} ]
}"""

prompt = f"""당신은 글로벌 원자재/거시경제 퀀트 애널리스트입니다.
아래 시장 입력 데이터를 바탕으로 6대 원자재(wti, copper, aluminum, gold, silver, platinum)의
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
- analogs 는 아래 '실제 과거 유사국면' 리스트의 각 항목당 1개씩 만드세요(리스트 순서·개수 그대로).
  period/similarity/actual/miniHist/miniForecast 는 주어진 값을 **그대로 복사**(임의 생성 금지),
  title(그 시기 실제 사건명)·summary 만 각 항목에 맞게 채우세요.
- advisor 는 최근 시황 → 글로벌 정세 → 주요 뉴스 → 구매 담당자 대응 조언 순의 3~4문장.
  우리회사는 초음파 진단기기를 만드는 회사고, 구매담당자들은 그 제품을 구성하는 원자재를 구매하고있음
  직접 구매하거나, 우리 협력사가 구매하는 자재에 해당 원자재들이 하위 n차 단계에서 사용되니 그 영향을 미리 전망하고
  원자재가 변동을 자재 단가에 적시 반영하는것이 중요함. 가격이 오를 전망이면 우리회사에 미칠 영향을 미리 전망/Risk 헷징 전략세우고
  가격이 떨어질 전망이면 떨어지는 시점에 완제품 자재 가격에 반영되는 원자재가격을 적시 반영하는 것이 중요함
- WTI의 경우 석유를 우리가 직접 사진 않지만, 석유로 만들어지는 플라스틱 커버류 (레진 소재), PE Foam/Pad 같은 포장재/비닐류의 영향이 큼
- 구리의 경우 프로브용 Raw Cable, 시스템용 일반 Cable (HDMI, BD to BD 등), Heatsink 등에 영향
- 알루미늄은 주로 시스템의 Frame, Bracket류 등 외장부품에 많이 사용됨
- 금은 Connector와 FPCB, PCB, Cable 등 다양한 곳에 사용되고 있고, 은도 일부 Connector에 사용됨
- 백금은 단결정(Single Crystal)의 생산과정에 설비에 사용되고 우리에게 직접 영향이 있지는 않음
- 단위: wti USD/bbl, copper·aluminum USD/ton, gold·platinum USD/oz.t, silver US￠/oz.t.

[실제 과거 유사국면 (실거래 데이터에서 상관분석으로 탐색됨)]
{json.dumps(analogs_real, ensure_ascii=False)}

[원자재별 주요 영향 요인]
{chr(10).join(f"- {k}: {v}" for k, v in FACTORS.items())}

[시장 입력 데이터]
{json.dumps(market_input, ensure_ascii=False)}

응답 스키마 (commodities 의 각 값은 아래 형태, name/unit 은 원자재에 맞게):
{{ "update_date": "{update_date}", "commodities": {{ "wti": {SCHEMA_ONE}, "copper": {{...}}, "aluminum": {{...}}, "gold": {{...}}, "silver": {{...}}, "platinum": {{...}} }} }}
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

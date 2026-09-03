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

import numpy as np
import pandas as pd

from _common import (
    COMMODITIES, META, PHI_HI, PHI_LO, build_history, fetch_raw, garch_sigma_path,
    latest_macro, load_calibration, load_manual_history, save_snapshot, today_str,
)

# 단계 C: 통계 밴드 분위계수. calibration.json 있으면 horizon별 (z_lo, z_hi),
# 없으면 정규 ±1.2816 항등. base(중앙값)는 보정 안 함 — band-only.
CALIB = load_calibration()
if CALIB:
    print(f"[진행] 밴드 캘리브레이션 로드: horizon {sorted(CALIB)} "
          f"(예: h6 z_hi={CALIB.get(6, (PHI_LO, PHI_HI))[1]:.2f} vs 정규 {PHI_HI})")

API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    sys.exit("[에러] GEMINI_API_KEY 환경변수가 없습니다.")

# 모델: 환경변수(GEMINI_MODEL, 쉼표구분)로 재정의 가능. 앞에서부터 순서대로 시도.
# flash-lite 를 1순위로 — 2026-09-02 현재 3.6-flash/flash-latest 는 배치 호출에도
# 504(DEADLINE)/503 가 잦고, flash-lite 만 안정적으로 빠르게 응답함.
MODELS = [m.strip() for m in os.environ.get("GEMINI_MODEL", "").split(",") if m.strip()] or [
    "gemini-flash-lite-latest",
    "gemini-3.6-flash",
    "gemini-flash-latest",
    # gemini-1.5·2.5 계열은 404, gemini-3.1-pro 는 무료티어 쿼터 0(429).
]

# 12종을 한 번에 요청하면 응답이 커서 MAX_TOKENS 로 잘리거나 504(DEADLINE)가 난다.
# → BATCH_SIZE 종씩 나눠 여러 번 호출하고 결과를 합친다.
BATCH_SIZE = 3
_MAX_OUT_TOKENS = 20000   # 배치당(3종) 출력은 ~7천 토큰 — 넉넉
_REQ_TIMEOUT_S = 100      # 배치 1회 호출 상한(초). 넘으면 그 모델은 포기하고 다음으로

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


def z_normalize(series):
    """형태(Shape) 비교를 위한 Z-Score 정규화 (평균 0, 표준편차 1)"""
    s = np.std(series)
    return np.zeros_like(series) if s == 0 else (series - np.mean(series)) / s


def dtw_distance_sq(s1, s2):
    """DTW (Dynamic Time Warping) 최소 누적 제곱 거리 계산"""
    n, m = len(s1), len(s2)
    dtw = np.full((n + 1, m + 1), np.inf)
    dtw[0, 0] = 0

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = (s1[i - 1] - s2[j - 1]) ** 2
            dtw[i, j] = cost + min(
                dtw[i - 1, j],      # 삽입 (시간 지연)
                dtw[i, j - 1],      # 삭제 (시간 단축)
                dtw[i - 1, j - 1]   # 매치
            )
    return dtw[n, m]


def top_analogs(key: str, win: int = 12) -> list[dict]:
    """현재 최근 `win`개월 '가격 궤적'과 모양·진폭이 닮은 과거 구간을 겹치지 않게
    유사도 내림차순으로 반환 (DTW 알고리즘 적용).
    """
    m, mult = _monthly_series(key)
    if m is None or len(m) < win + _FWD + win:
        return []
        
    excl = max(4, win * 2 // 3)
    cur = m.iloc[-win:]
    cur_path = (cur / cur.iloc[0] * 100.0).to_numpy()
    cur_amp = float(cur_path.max() - cur_path.min())

    # 현재 궤적 정규화 (형태만 추출)
    cur_z = z_normalize(cur_path)

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

        # DTW 기반 형태(Shape) 유사도 산출
        wp_z = z_normalize(wp)
        dist_sq = dtw_distance_sq(cur_z, wp_z)
        
        # 유클리드 거리와 피어슨 상관계수의 관계식(r = 1 - SSE/2n)을 응용
        shape_sim = max(0.0, 1.0 - (dist_sq / (2.0 * win)))

        # 진폭(Amplitude) 크기 차이에 대한 페널티
        amp = float(wp.max() - wp.min())
        amp_ratio = min(amp, cur_amp) / max(amp, cur_amp) if max(amp, cur_amp) else 0.0
        
        # 최종 유사도: 형태 60% + 진폭 40%
        sim = shape_sim * (0.6 + 0.4 * amp_ratio)
        
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


def calculate_statistical_bounds(key: str, months_ahead: int = 6) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """중심선 = 감쇠 드리프트(랜덤워크에 가깝게), 밴드 = 변동성 term-structure ×
    분위계수. 변동성은 GARCH(1,1) h개월 누적(단계 F, garch_sigma_path). 분위계수는
    정규 ±1.2816 이 기본이나 calibration.json(단계 C, split-conformal)이 있으면
    horizon별 실측 잔차분위(z_lo, z_hi)로 대체 — 팩테일·상하방 비대칭 반영.
    base(중앙값)는 불변. 근거·백테스트는 backtest.py 및 그 하단 가정 블록 참고.
    """
    m_raw, mult = _monthly_series(key)
    spot_price = spot.get(key)
    
    # 최소 12개월 이상의 데이터가 필요함 (없으면 기본값으로 대체)
    if m_raw is None or len(m_raw) < 12:
        # 데이터가 부족하면 기본 spot 값에 연간 변동성 25% 가정하여 선형 밴드 구축
        s_price = spot_price or 100.0
        vol_array = 0.10 * np.sqrt(np.arange(1, months_ahead + 1))
        base_path = s_price * np.ones(months_ahead)
        bull_path = s_price * (1.0 + 1.28 * vol_array)
        bear_path = s_price * (1.0 - 1.28 * vol_array)
        return base_path, bull_path, bear_path

    m = m_raw * mult
    if not spot_price:
        spot_price = m.iloc[-1]

    # 드리프트: 최근 12개월 평균 로그수익률의 20%만 반영(추세 과신 억제)
    m_12 = m.iloc[-12:] if len(m) > 12 else m
    log_returns_12 = np.log(m_12 / m_12.shift(1)).dropna()
    drift = log_returns_12.mean() * 0.20 if len(log_returns_12) > 0 else 0.0

    # 단계 F: 밴드 변동성 term-structure = GARCH(1,1) h개월 누적. √h(월간분산
    # 일정 가정)는 최근 국면에 둔감하고 장기 커버리지가 무너진다(백테스트로
    # 확인 — garch+C 가 sqrt+C 대비 pinball·커버리지·나이브대비스킬 모두 개선).
    # 데이터 부족·GARCH 실패 시 helper 가 √h(하한 1.5%)로 폴백.
    m_lr = np.log(m / m.shift(1)).dropna().to_numpy()
    sig_h = garch_sigma_path(m_lr, months_ahead, 0.015)

    base_path = np.zeros(months_ahead)
    bull_path = np.zeros(months_ahead)
    bear_path = np.zeros(months_ahead)

    # 분위계수는 calibration.json(단계 C) 있으면 horizon별 실측치, 없으면 정규
    # ±1.2816. base(중앙값)는 항상 모델값 그대로.
    for t in range(1, months_ahead + 1):
        mean_log = np.log(spot_price) + drift * t
        sigma_t = float(sig_h[t - 1])
        z_lo, z_hi = CALIB.get(t, (PHI_LO, PHI_HI))

        base_path[t - 1] = np.exp(mean_log)
        bull_path[t - 1] = np.exp(mean_log + z_hi * sigma_t)
        bear_path[t - 1] = np.exp(mean_log + z_lo * sigma_t)

    return base_path, bull_path, bear_path


analogs_real = {k: top_analogs(k, 12) for k in COMMODITIES}      # 1년 비교
analogs_real_6m = {k: top_analogs(k, 6) for k in COMMODITIES}     # 6개월 비교


update_date = today_str()
macro = latest_macro(raw)

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
                "중국 광산 품위 저하, 스크랩(초경 재생) 회수율, 미국·EU 전략비축·공망 다변화",
    "silicon": "매크로: 중국 제조업 PMI, 글로벌 철강 수요 (합금철 원료), 석탄 및 전력 단가 (제조 에너지 비용), 달러·위안화. "
               "마이크로: Ferro Silicon (FeSi 75%) 중국 생산량 및 가동률, 중국 수출 관세 정책, 주요 철강 제련소(포스코 등) 계약단가 추이, "
               "중국 FOB 선적 요율, 규석(원료) 및 전극봉 가격, 글로벌 자동차 알루미늄 다이캐스팅 수요",
    "hbeam": "매크로: 국내 건설·토목 착공 면적, 부동산 PF 및 SOC 예산, 기준금리·건설 경기. "
        "마이크로: 국내 전기로 제강사(현대제철·동국제강) 가동률 및 판매가격 인상/할인 정책, "
        "고철(철스크랩) 투입원가 스프레드, 수입산(중국/일본/베트남) 형강 유입량 및 유통재고",
    "crc": (
        "매크로: 글로벌 자동차·가전 생산량, 중국 조강생산 및 수출세/환급 정책, 환율(달러/원, 위안화). "
        "마이크로: 열연코일(상위 원자재) 가격 변동 전가 속도, 포스코/현대제철 냉연 유통 출하가, "
        "가전·외장 판금용 SPCC 수요, 아시아 MEPS 현물가, 냉연-열연 스프레드(가공마진)"
    ),
    "scrapheavya": (
        "매크로: 국내외 조강 생산량(전기로 비중), 글로벌 철스크랩 해상 운임, 경기 선행지수. "
        "마이크로: 국내 제강사 고철 구매 단가 및 재고 일수, 일본(H2)·미국(HMS) 수입 고철 오퍼가, "
        "건설 해체/철거 물량 및 유통상 재고, 전기로 가동률"
    ),
    "scrapprime": (
        "매크로: 자동차·가전 완성품 제조공장 가동률, 제조업 생산지수. "
        "마이크로: 프레스 가공 부산물(생철) 발생량, 고급 판재류 전기로 투입 수요, "
        "중량A 대비 생철 프리미엄 스프레드, 판금/차체 협력사 스크랩 단가 환급 추이"
    ),
    "wirerod": (
        "매크로: 글로벌 인프라/건설 투자, 자동차 볼트/너트·와이어로프·타이어코드 수요. "
        "마이크로: 빌릿(Billet) 및 슬래브 가격 추이, 주요 제선/제강사 선재 공장 가동률, "
        "중국산 저가 선재 수출 압력, CHQ(냉간압조용) 선재 스프레드"
    ),
    "sts304": "매크로: LME 니켈 가격, 중국 300계열 스테인리스 생산량 및 재고, 글로벌 경기. "
        "마이크로: 포스코 STS 출하가 및 알로이 서차지(Alloy Surcharge) 변동, STS 스크랩 가격, "
        "인도네시아/중국산 STS 수입재 유통 가격, 정밀 의료기기/외장재 가공 수요",
    "hrc": "매크로: 중국 부동산·인프라 투자, 글로벌 제조업/건설 PMI, 달러·위안화, 각국 관세·반덤핑(美 232조, EU CBAM). "
          "마이크로: 철광석·원료탄(코킹콜) 가격, 中 조강생산 통제·감산 지침, 중국 철강 수출량·수출증치세 환급, "
          "美 중서부 HRC 스프레드, 전기로 vs 고로 가동률, 자동차·가전·조선 수요, 유통재고·리드타임, "
          "우리회사 관점: 시스템 Frame/Bracket 및 협력사 판금 가공품(SPCC 냉연) 단가에 후행 반영",
}

SCHEMA_ONE = """{
  "name": "WTI 원유 (CME)", "unit": "USD/bbl",
  "current_price": 0.0, "forecast_6m_target": 0.0, "forecast_change_rate": "+0.0%", "volatility_score": 0,
  "planning_advisor": "구매/헤지 담당자를 위한 한 문장 전략 코멘트",
  "advisor": "원자재 구매 담당자를 위한 3~4문장. 최근 시황 / 관련 글로벌 정세 / 알아야 할 주요 뉴스 / 대응 조언 순서로 서술.",
  "monthly_forecast_base": [ {"month": "2026-10", "price": 0.0, "rationale": "해당 월 기본 시나리오 근거 한 문장"}, "…(2026-11 … 2027-03 까지 총 6개)" ],
  "monthly_forecast_bull": [ {"month": "2026-10", "price": 0.0, "rationale": "해당 월 상방 요인 중심 근거 한 문장"}, "…(총 6개)" ],
  "monthly_forecast_bear": [ {"month": "2026-10", "price": 0.0, "rationale": "해당 월 하방 요인 중심 근거 한 문장"}, "…(총 6개)" ],
  "rationale_base": "기본 시나리오 요약", "rationale_bull": "낙관 요약", "rationale_bear": "비관 요약",
  "metrics": [ {"label": "위안화 환율", "val": "6.7222 (USD/CNY)", "date": "2026.08.27", "cat": "수요", "status": "보통", "badge": "secondary"} ],
  "analogs": [ {"title": "그 시기에 실제 있었던 역사적 사건명", "summary": "그 국면의 수급/매크로 배경 요약"} ],
  "analogs_6m": [ {"title": "그 시기 실제 사건명", "summary": "그 국면 배경 요약"} ]
}"""

def build_prompt(keys: list[str]) -> str:
    """commodity 부분집합(keys)에 대한 프롬프트 생성. 배치 호출용."""
    keys_str = ", ".join(keys)
    schema_keys = ", ".join(
        f'"{k}": {SCHEMA_ONE}' if i == 0 else f'"{k}": {{...}}'
        for i, k in enumerate(keys)
    )
    market_input = {
        "update_date": update_date,
        "macro": macro,
        "current_spot": {k: spot.get(k) for k in keys},
        "history_summary": {k: history_brief.get(k) for k in keys},
    }
    ar = {k: analogs_real.get(k, []) for k in keys}
    ar6 = {k: analogs_real_6m.get(k, []) for k in keys}
    factors_txt = chr(10).join(f"- {k}: {FACTORS[k]}" for k in keys if k in FACTORS)
    return f"""당신은 글로벌 원자재/거시경제 퀀트 애널리스트입니다.
아래 시장 입력 데이터를 바탕으로 원자재 {len(keys)}종({keys_str})의
6개월 가격 전망 데이터셋을 순수 JSON 으로만 작성하세요. 마크다운/설명 금지.

규칙:
- monthly_forecast_base / monthly_forecast_bull / monthly_forecast_bear 는 각각
  **정확히 6개 원소**의 배열이어야 합니다({update_date} 다음 달부터 연속 6개월,
  예: 2026-10, 2026-11, 2026-12, 2027-01, 2027-02, 2027-03). 5개 이하는 무효.
- 세 배열 모두 각 월에 price(숫자)와 rationale(한 문장)을 넣으세요.
  bull 은 상방 요인, bear 는 하방 요인 중심으로 근거를 서술.
- current_price 는 위 current_spot 값(최근 실적가)과 동일하게, base 첫 달은 거기서 ±4% 이내 출발.
- base 경로는 '직선'이 아니라 실제 전망처럼 **월별로 방향 전환·되돌림·기울기 변화**가
  나타나야 합니다. 참고 근거:
  · 위 '실제 과거 유사국면'의 miniForecast(그 국면 이후 실제 6개월 궤적)의 '모양'을 참고
    (그대로 복사 말고, 현재 매크로/컨센서스에 맞춰 조정).
  · 알려진 이벤트·계절성(OPEC+ 회의, 재고 사이클, 중국 정책 시점, FOMC 등)을 월에 반영.
  · 방향이 바뀔 근거가 있으면 그 달에 고점/저점을 만들어도 됩니다. 단일 방향 6연속 지양.
- 각 월에서 bear.price < base.price < bull.price 는 반드시 유지. bull/bear 는 base 대비
  불확실성으로, 시간이 갈수록 스프레 벌어지되 이벤트 리스크가 큰 달은 더 크게.
- 6개월 누적 변화폭은 대체로 ±25% 이내(초강세/초약세 국면이면 근거와 함께 초과 가능).
- badge 는 danger/warning/success/secondary 중 하나. cat 은 공급/수요/투자/매크로 중 하나.
- metrics 는 각 원자재의 아래 '주요 영향 요인' 중 현시점에서 가격에 영향이 큰 것 위주로
  6~8개 선정하고(매크로·마이크로 균형있게), label 에 지표명, val 에 최신 추정치 and 단위,
  cat(공급/수요/투자/매크로)·status(강세/보통/약세)·badge 를 채우세요.
- analogs 는 아래 '실제 과거 유사국면(1년)', analogs_6m 은 '(6개월)' 리스트의 각 항목당
  title 과 summary 만 채운 객체 1개씩(리스트 순서·개수 그대로, 비어 있으면 빈 배열).
  period·similarity·actual·miniHist·miniForecast 같은 수치는 시스템이 실제값으로
  채우므로 **출력하지 마세요**. title=그 시기 실제 사건명, summary=그 국면 배경.
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
- 철강/판금류: 열연코일(HRC) → 냉연코일(CRC/SPCC) → 판금 가공품으로 1~2개월 시차를 두고 전가됨.
- 스테인리스(STS304): 니켈 가격과 포스코 알로이 서차지 변동에 직접 연동되므로 단기 변동성 추적이 필수적임.
- 고철(스크랩): 철강 제품 가격의 가장 강력한 선행 지표이자, 협력사 가공 시 발생하는 스크랩 매각/환급 단가 정산에 직접 활용됨.
- H형강/선재: 대형 구조물 프레임 및 볼트/너트/파스너 등 기초 체결류 단가 산정의 기준 지표임.
- 실리콘은 Ferro Silicon 합금철 원료로, 프레임/브라켓 등 외장부품 제작에 간접 반영됨.
- 단위: - 단위: wti USD/bbl, copper·aluminum·nickel·zinc·silicon·cold_rolled_coil·wire_rod·hot_rolled_coil USD/ton, gold·platinum USD/ozt, silver US￠/ozt, tungsten RMB/mt, ironore USD/ton, 
  h_beam_small_medium·scrap_heavy_a·scrap_prime·sts304_cr_2mm KRW/ton.
- 구리, 알루미늄, 금, 은의 경우 케이블, 히트싱크, 커넥터, 프레임 등 협력사 단가에 즉각 반영되는 품목임. 공급망 차질뉴스 (광산 파업, 제련소 이슈)에 매우 민감하게 반응하도록 
  가중치를 더 부여할 필요가 있고, 단기 급등시 선제 구매를 통한 헷징의 필요성이 있음
- 철광석, 열연강판, WTI 등은 프레임, 브라켓 단가로 반영되기까지 2개월 정도의 시차가 있음. 가격 하락기에 진입할 때엔 협력사의 단가 인하 협상을 준비할 타이밍을 구체적으로 짚어주는 것이 중요함
- 텅스텐, 니켈, 아연은 중국의 수출통제나 특정 국가(인도네시아 등)의 정책에 따라 가격이 요동침. 매크로 지표보다 지정학적 리스크와 수출 통제 뉴스가 발생할 경우 Bull Band 상단으로 극단적인 상향조정 하는 규칙이 필요함.
- 실리콘은 중국의 전력 규제나 합금철 공장 가동률, 석탄 가격에 따라 단기 가격 왜곡이 크므로 에너지 뉴스 모니터링이 필수적임을 명시하십시오.
- 최근 변동성 가중(EMA)를 반영하여, Bull/Bear 밴드는 최근 3~6개월의 변동성에 더 큰 가중치를 두는 지수이동평균을 사용하거나 최근 12개월 변동성을 혼합하여, 최근 원자재가격이
  급등락했을 경우, 다음달의 Bull/Bear밴드가 즉각적으로 넓어져 실제 시장의 불확실성을 더 잘 반영하게 해야

[실제 과거 유사국면 (1년) — 실거래 가격궤적 유사도 분석]
{json.dumps(ar, ensure_ascii=False)}

[실제 과거 유사국면 (6개월) — 실거래 가격궤적 유사도 분석]
{json.dumps(ar6, ensure_ascii=False)}

[원자재별 주요 영향 요인]
{factors_txt}

[시장 입력 데이터]
{json.dumps(market_input, ensure_ascii=False)}

응답 스키마 (commodities 의 각 값은 아래 형태, name/unit 은 원자재에 맞게):
{{ "update_date": "{update_date}", "commodities": {{ {schema_keys} }} }}
"""


def _extract_json(resp) -> dict:
    """SDK 응답 → dict. 코드펜스·앞뒤 잡텍스트·잘림 방어, 실패 시 원인을 메시지에."""
    txt = (getattr(resp, "text", None) or "").strip()
    fr = None
    try:
        fr = str(resp.candidates[0].finish_reason)
    except Exception:  # noqa: BLE001
        pass
    if not txt:
        raise ValueError(f"빈 응답 (finish_reason={fr})")
    if txt.startswith("```"):
        txt = txt[3:]
        if txt[:4].lower() == "json":
            txt = txt[4:]
        txt = txt.split("```", 1)[0]
    i, j = txt.find("{"), txt.rfind("}")
    if i >= 0 and j > i:
        txt = txt[i:j + 1]
    try:
        return json.loads(txt)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 파싱 실패: {e} · finish_reason={fr} · 길이 {len(txt)}") from e


def call_gemini(text: str) -> dict:
    """MODELS 를 순서대로 1회씩만 시도(504/503/파싱실패는 재시도해도 잘 안 풀리고
    시간만 먹으므로 곧장 다음 모델로). 파싱 실패한 응답은 한 번 더만 재시도."""
    last = None
    for model in MODELS:
        for attempt in (1, 2):
            t0 = time.monotonic()
            try:
                print(f"[진행] {model} 호출…")
                if _NEW_SDK:
                    cfg = {
                        "response_mime_type": "application/json",
                        "automatic_function_calling": {"disable": True},  # AFC 경고 억제
                        "temperature": 0.35,  # 월별 변동은 살리되 실행 간 안정은 clamp_vs_previous 로
                        "top_p": 0.9,
                        "max_output_tokens": _MAX_OUT_TOKENS,
                        "http_options": {"timeout": _REQ_TIMEOUT_S * 1000},  # ms
                    }
                    if "thinking" in model:
                        cfg["thinking_config"] = {"thinking_budget": 0}  # 사고 지연 제거
                    r = _client.models.generate_content(
                        model=model, contents=text,
                        config=types.GenerateContentConfig(**cfg),
                    )
                    out = _extract_json(r)
                else:
                    m = _legacy.GenerativeModel(model)
                    r = m.generate_content(
                        text,
                        generation_config={
                            "response_mime_type": "application/json",
                            "temperature": 0.35, "top_p": 0.9,
                            "max_output_tokens": _MAX_OUT_TOKENS,
                        },
                        request_options={"timeout": _REQ_TIMEOUT_S},
                    )
                    out = _extract_json(r)
                print(f"[진행] {model} 응답 {time.monotonic() - t0:.0f}s")
                return out
            except Exception as e:  # noqa: BLE001
                last = e
                dt = time.monotonic() - t0
                print(f"[경고] {model} {dt:.0f}s 실패: {e}")
                # 파싱 실패(응답은 왔음)만 같은 모델로 1회 재시도, 그 외는 곧장 다음 모델
                if attempt == 1 and "파싱 실패" in str(e):
                    time.sleep(2)
                    continue
                break
    raise RuntimeError(f"Gemini 전체 실패: {last}")


def _n(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def calculate_ewma_vol_percentile(key: str) -> float:
    """최근 24개월(2년)간의 일간 로그수익률 데이터를 기반으로 EWMA(지수가중이동평균, lambda=0.94)
    연율화 변동성을 산출한 후, 최근 2년 변동성 분포 대비 현재 변동성의 백분위(Percentile, 0~100%)를 계산합니다.
    """
    # 1. 일간 가격 데이터 확보
    s = raw.get(key)
    if s is not None and len(s) > 20:
        prices = s.dropna()
    else:
        # 야후에 없거나 수동 데이터인 경우 history rows 사용
        rows = history.get(key) or []
        if len(rows) < 20:
            return 50.0  # 데이터가 너무 부족하면 보통(50%)으로 반환
        prices = pd.Series(
            {pd.Timestamp(r["date"]): float(r["price"]) for r in rows if r.get("price")}
        ).sort_index().dropna()

    # 2. 일간 로그수익률 산출
    returns = np.log(prices / prices.shift(1)).dropna()
    if len(returns) < 10:
        return 50.0

    # 3. EWMA 분산 계산 (RiskMetrics 표준 lambda=0.94)
    decay = 0.94
    ewma_var = returns.pow(2).ewm(alpha=1 - decay, adjust=False).mean()
    ewma_vol_daily = np.sqrt(ewma_var)
    ewma_vol_annual = ewma_vol_daily * np.sqrt(252) * 100.0  # 백분율화 (%)

    # 4. 최근 2년(약 504 영업일) 데이터 추출
    recent_vol = ewma_vol_annual.iloc[-504:] if len(ewma_vol_annual) > 504 else ewma_vol_annual
    if len(recent_vol) < 2:
        return 50.0

    current_vol = recent_vol.iloc[-1]
    
    # 5. 백분위(Percentile) 산출
    less_equal_count = np.sum(recent_vol <= current_vol)
    percentile = (less_equal_count / len(recent_vol)) * 100.0
    return round(percentile, 1)


def track_forecast_direction(commodities: dict) -> None:
    """기존 raw_materials_forecast.json의 이전 전망 기조와 비교하여
    예측 기조의 연속성(상승/하강 유지 또는 전환 발생) 및 연속 개수(Streak)를 계산합니다.
    """
    prev_data = {}
    try:
        if os.path.exists("raw_materials_forecast.json"):
            with open("raw_materials_forecast.json", encoding="utf-8") as f:
                prev_data = json.load(f).get("forecast_data") or {}
    except Exception:  # noqa: BLE001
        pass

    for k in COMMODITIES:
        c = commodities.get(k)
        if not c:
            continue
            
        cur_price = c.get("current_price") or 1.0
        target_price = c.get("forecast_6m_target") or cur_price
        
        # 현재 전망 기조 판정 (상승 / 하강 / 보합)
        if target_price > cur_price * 1.001:  # 0.1% 이상 상승 시 상승 기조
            cur_stance = "상승"
        elif target_price < cur_price * 0.999:  # 0.1% 이상 하락 시 하강 기조
            cur_stance = "하강"
        else:
            cur_stance = "보합"

        # 이전 데이터 조회
        p = prev_data.get(k) or {}
        has_prev = "direction_stance" in p
        prev_stance = p.get("direction_stance") or "상승"
        prev_streak = p.get("direction_streak") or 1
        
        # 상태 기조 변화 판정 및 Streak 업데이트
        if not has_prev:
            # 이전 기록이 전혀 없는 첫 실행인 경우 각 자산의 실제 계산된 기조로 1개월 시작
            new_streak = 1
            status_text = f"{cur_stance}방향 유지"
        elif cur_stance == prev_stance:
            new_streak = prev_streak + 1
            status_text = f"{cur_stance}방향 유지"
        else:
            new_streak = 1
            status_text = f"전환 발생({prev_stance}➡️{cur_stance})"
            
        # 신규 필드 적재
        c["direction_stance"] = cur_stance
        c["direction_streak"] = new_streak
        c["direction_status"] = status_text


def sanitize_scenarios(commodities: dict) -> None:
    """통계와 AI 의 하이브리드 예측 결합 모델(Forecast Combination)을 실행합니다.
    1. calculate_statistical_bounds 로 통계적 base/bull/bear 산출
       (드리프트 12개월×0.2, 변동성 GARCH(1,1) 누적[단계 F], 분위계수 calibration.json[단계 C]).
    2. AI 가 제시한 base 가격과 통계적 base 가격을 50:50으로 블렌딩 (가중치 0.5).
    3. 블렌딩된 base 가 통계적 bull/bear 밴드를 이탈하지 않도록 클리핑.
    4. 최종 bull/bear 끝점은 순수 통계값(최종 밴드)으로 고정하여 자산 고유의 변동성을 반영.
    """
    for k in COMMODITIES:
        c = commodities.get(k)
        if not c:
            continue
            
        base_ai = c.get("monthly_forecast_base") or []
        bull_ai = c.get("monthly_forecast_bull") or []
        bear_ai = c.get("monthly_forecast_bear") or []
        if not base_ai:
            continue
            
        cur = spot.get(k) or _n(c.get("current_price"))
        if not cur or cur <= 0:
            cur = _n(base_ai[0].get("price"), 1.0)
        c["current_price"] = round(cur, 2)
        
        # 1단계: 역사적 데이터를 기반으로 통계적 중심선 및 밴드 계산
        stat_base, stat_bull, stat_bear = calculate_statistical_bounds(k, months_ahead=6)
        
        # 2단계: AI 경로 블렌딩 및 밴드 내 고정
        prev_price = cur
        for i, row in enumerate(base_ai):
            ai_val = _n(row.get("price"), prev_price)
            # 월별 변동폭 ±30% 제한 장치
            ai_val = min(max(ai_val, prev_price * 0.7), prev_price * 1.3)
            
            s_base = stat_base[i]
            s_bull = stat_bull[i]
            s_bear = stat_bear[i]
            
            # 50:50 블렌딩 (통계 50% + AI 50%)
            blended_base = 0.5 * ai_val + 0.5 * s_base
            
            # 통계적 밴드를 벗어나지 못하도록 제한 (최소 1.5% 완충지대 확보)
            buffer = s_base * 0.015
            clamped_base = min(max(blended_base, s_bear + buffer), s_bull - buffer)
            
            row["price"] = round(clamped_base, 2)
            prev_price = clamped_base
            
            # 최종 bull/bear 에는 자산별 실제 변동성이 반영된 통계적 끝값 주입
            if i < len(bull_ai):
                bull_ai[i]["price"] = round(max(s_bull, clamped_base + buffer), 2)
            if i < len(bear_ai):
                bear_ai[i]["price"] = round(min(s_bear, clamped_base - buffer), 2)
                
        # 3단계: 6개월 타겟값 및 변화율 재산출
        last_price = base_ai[-1]["price"]
        c["forecast_6m_target"] = round(last_price, 2)
        c["forecast_change_rate"] = f"{(last_price / cur - 1) * 100:+.1f}%"


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
        for arr in ("monthly_forecast_base", "monthly_forecast_bull", "monthly_forecast_bear"):
            if len(commodities[k].get(arr) or []) < 5:
                raise ValueError(
                    f"{k} {arr} 가 {len(commodities[k].get(arr) or [])}개월 (6개월 필요)")


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


_BATCHES = [COMMODITIES[i:i + BATCH_SIZE] for i in range(0, len(COMMODITIES), BATCH_SIZE)]
print(f"[진행] Gemini 전망 생성… ({len(COMMODITIES)}종 → {len(_BATCHES)}개 배치)")
try:
    def _batch_defects(got: dict, chunk: list) -> list[str]:
        """배치 응답에서 누락·월수 부족(<5)인 품목 목록. flash-lite 가 '6개월'
        지시를 무시하고 1~2개만 내는 경우가 잦아 배치 단위로 걸러 재시도한다."""
        bad = []
        for k in chunk:
            c = got.get(k)
            if not isinstance(c, dict):
                bad.append(k)
                continue
            if any(len(c.get(a) or []) < 5 for a in
                   ("monthly_forecast_base", "monthly_forecast_bull", "monthly_forecast_bear")):
                bad.append(k)
        return bad

    commodities: dict = {}
    for bn, chunk in enumerate(_BATCHES, 1):
        got = {}
        for attempt in range(1, 4):
            print(f"[진행] 배치 {bn}/{len(_BATCHES)}: {', '.join(chunk)}"
                  + (f" (재시도 {attempt - 1})" if attempt > 1 else ""))
            parsed = call_gemini(build_prompt(chunk))
            got = parsed.get("commodities") or parsed
            bad = _batch_defects(got, chunk)
            if not bad:
                break
            print(f"[경고] 배치 {bn}: {bad} 응답 누락/월수부족 → 재시도")
        bad = _batch_defects(got, chunk)
        if bad:
            raise ValueError(f"배치 {bn} 재시도 후에도 불량: {bad}")
        for k in chunk:
            commodities[k] = got[k]
    validate(commodities)
    fix_months(commodities)          # 월 라벨을 연속 6개월로 강제(순서 꼬임 방지)
    fix_analogs(commodities)         # 유사국면 수치는 실제값 강제, AI 는 title/summary 만
    clamp_vs_previous(commodities)   # 실행 간 급변만 억제(경로 모양 유지)
    sanitize_scenarios(commodities)  # 이상치·역전만 최소 보정 + target 재계산
    
    # EWMA 변동성 백분위 산출 및 전망 방향성 추적 적용
    for k in COMMODITIES:
        c = commodities.get(k)
        if c:
            c["volatility_score"] = calculate_ewma_vol_percentile(k)
            
    track_forecast_direction(commodities)
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

# 이번 배치 전망을 이력으로 동결 보관 + 지나간 예측월을 실제가와 대조(정확도 원장)
save_snapshot(update_date, macro, commodities, history)

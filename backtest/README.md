# backtest/ — 통계 전망 워크포워드 백테스트 (단계 A)

`../backtest.py` 가 만드는 산출물. **live 파이프라인과 완전히 분리**된 분석용이며,
`raw_materials_forecast.json` 의 `history_3y` 만 읽는다(네트워크·Gemini 불필요).

## 무엇을 하나

과거를 한 달씩 굴리며, 각 시점 t 에서 **그때까지의 데이터만** 으로 1~6개월
전망을 만들어 이후 실제가와 대조한다(look-ahead 없음). 전망 방법론은
`analyze.py` 의 `calculate_statistical_bounds` 재현(감쇠 드리프트 + √h 로그정규
밴드). Gemini 블렌드는 재현 불가라 제외 → **통계 뼈대 단독의 스킬 = 전체
전망 품질의 하한선**.

## 실행

```bash
python backtest.py                                   # 기본(현행 analyze.py 파라미터)
python backtest.py --drift-damping 0.3 --vol-long 24 # 파라미터 스윕
python backtest.py --no-csv                          # 집계만
```

## 산출물

| 파일 | 내용 |
|---|---|
| `results.json` | 설정·데이터범위 + `overall` / `by_horizon` / `by_commodity` 집계 + PIT 히스토그램 |
| `records.csv` | 채점된 전망 건별(품목·전망월·타겟월·h·분위·실제·오차·pinball·PIT·나이브 3종 비교) |

## 지표 읽는 법

- **pinball_pct** — 분위손실 평균(현재가 대비 %). 낮을수록 좋음. 점추정만 보는
  MAE 와 달리 밴드 폭까지 벌점하는 proper scoring rule. 품목 간 합산의 주 지표.
- **mae_pct / bias_pct** — 중앙값 절대오차 / 부호오차. `bias>0` = 전망이 실제보다
  체계적으로 높음(과대). 단계 C 편향 보정량의 근거.
- **cov80 / cov50** — 80%·50% 밴드의 실제 커버리지. 목표 0.80 / 0.50.
  낮으면 밴드가 좁다(과신) → 단계 C 캘리브레이션 배율.
- **skill_vs_naive** — `1 - pinball_model / pinball_naive`. **>0 이어야 '현재가
  유지'보다 값어치 있음.** 원자재 6개월 예측에서 나이브 이기기는 실제로 어렵다.
- **dm_vs_naive_stat / _p** — Diebold-Mariano(HLN 보정). `|stat|>1.96, p<0.05`
  이면 스킬 차이가 통계적으로 유의. 아니면 "나이브와 구별 안 됨".
- **pit_ks_D / _p**, **pit_histogram** — 예측분포 캘리브레이션. 히스토그램이
  10칸 균등이면 양호. 가운데 볼록=밴드 과대, U자=밴드 과소, 한쪽 끝 몰림=
  전망 편향(오른쪽 끝 몰림 = 전망이 상방을 못 따라감).

## 현재 기준선 (2026-09-02, 기본 파라미터)

- 나이브 대비 스킬 ≈ 0 (DM p≈0.56) → **감쇠 드리프트가 랜덤워크 대비 사실상
  개선 없음.** 중심 전망을 개선하려면 평균회귀·앙상블(단계 D)이 필요.
- cov80 ≈ 0.75 (h=1 0.80 → h=6 0.72) → **밴드가 약간 좁고, 장기로 갈수록 더
  좁다.** √h 스케일의 한계 → 캘리브레이션(C) 또는 GARCH term-structure(F).
- PIT 오른쪽 끝 과대 + 금속류 bias 음수 → 2019~2026 상승장을 전망이 과소추종.

이 숫자들이 단계 B~F 의 "개선했다" 판정 기준(baseline)이다.

## 단계 C — split-conformal 밴드 캘리브레이션 (완료)

```bash
python backtest.py --calibrate --center model            # 보정 전후 비교
python backtest.py --calibrate --emit-calibration        # calibration.json 생성
```

정규 Φ⁻¹(p) 대신 **표준화 잔차의 실측 p-분위**로 밴드를 뽑는다(분포가정 없음).
롤링 OOS(각 시점 캘리브레이션은 그 이전 실현 잔차로만 적합, 워밍업 40개).

| | 기준 | 보정(center=model) |
|---|---|---|
| cov80 | 0.755 (h1 .80 → h6 .72) | **0.789** (h별 .78~.81 평탄) |
| cov50 | 0.499 | 0.516 |
| MAE% | 11.65 | **11.65 (불변)** |
| pinball% | 4.038 | 4.104 (+1.6%) |

- **`center=model` 채택**: 중앙값(점전망)은 모델값 유지, 스프레드만 보정.
  `center=conformal`(중앙값도 이동)은 MAE 11.65→12.65, bias +1.6%p 로 악화 —
  편향 보정은 국면 의존적이라 일반화 안 됨.
- pinball +1.6% 는 "당초 무회귀 게이트" 탈락이지만 의도적 수용: pinball 은
  밴드가 좁을수록 유리한 지표라 명목 커버리지와 상충하고, 이 제품(구매·헤지
  bull/bear)의 목적은 "80% 밴드가 실제 80%" 이다. 중앙값·pinball 개선은 단계 D.
- 산출 `../calibration.json` → `analyze.py calculate_statistical_bounds` 가
  horizon별 (z_lo, z_hi) 로 읽어 적용. 파일 없으면 정규 ±1.2816 항등.
- 캘리브레이션이 잡아낸 것: **상하방 비대칭**(h6 상방 z=1.75 vs 하방 −1.22 =
  공급쇼크 급등 리스크가 수요약세 하락보다 두껍다) + **장기 팩테일**.

## 단계 D — 통계 앙상블 → 음성결과 (production 미적용)

```bash
python backtest.py --ensemble          # 멤버 단독 MAE + equal/invmae 앙상블 비교
python backtest.py --emit-ensemble     # ensemble.json (미커밋 — 분석용)
```

멤버 6종(stat·naive·drift·season·meanrev·mom)을 1/MAE 가중(롤링 OOS)으로 결합.

| | MAE% (전 horizon) |
|---|---|
| **naive (랜덤워크)** | **11.48 — 전 horizon 최저** |
| stat (현행 production) | 11.65 |
| meanrev | 11.98 |
| momentum | 13.26 |
| drift(무감쇠) | 13.75 |
| seasonal | 17.01 |
| ens_equal / ens_invmae | 12.18 / 12.11 (baseline·naive 보다 악화) |

- **랜덤워크가 최선.** 뭘 섞어도 노이즈만 추가 (멤버 간 상관 높아 분산화 이득
  없음, non-naive 멤버는 추세장 편향). 상품 spot 이 월단위에서 랜덤워크에
  가깝다는 기존 실증과 일치.
- **결론**: 통계 중심선은 현행 유지. 전망의 값어치는 AI/뉴스 레이어 + 단계 C
  밴드에 있다. 앙상블 코드는 분석 도구로만 남김. 다음은 밴드·구조(공통인자·
  GARCH) 쪽.
- 부수 발견: `--drift-damping 0`(중심선을 순수 naive 로)이 현행 0.2보다 OOS
  MAE 0.17%p 낮음. 작지만 "드리프트를 더 줄여라" 방향.

앞으로 어떤 변경도 `results.json` 의 pinball/커버리지가 여기서 더 나빠지면 채택 안 함.

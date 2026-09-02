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

이 숫자들이 단계 B~F 의 "개선했다" 판정 기준(baseline)이다. 어떤 변경도
`results.json` 의 pinball/커버리지가 **이보다 나빠지면 채택하지 않는다.**

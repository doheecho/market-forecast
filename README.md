# 원자재 시황 — 핵심 원자재 AI 가격 전망

WTI·전기동·알루미늄·금·은·백금·열연강판·철광석·니켈·아연·텅스텐·실리콘의
6개월 가격 전망(Base/Bull/Bear) 대시보드.

- **시세 소스**: `manual/*.csv` 가 **1차 소스**입니다. CSV 가 있는 품목은 그 값으로
  차트·통계·유사국면·전망을 만들고, CSV 가 없거나 20행 미만인 품목(현재는 `steel`)만
  **야후 파이낸스로 폴백**합니다. (자세히는 아래 "시세 CSV" 참고)
- **갱신 주기**: **AI 전망은 주 1회(월요일)** 만 돌립니다. CSV 를 고쳐도 전망은 다시
  돌지 않고 **시세만** 반영됩니다. 매크로·`steel` 시세는 평일 매일 갱신됩니다.
- **전망 이력·정확도**: 매주 월요일 배치마다 그날 전망을 `snapshots/forecast/<날짜>.json`
  으로 동결 보관하고, 이미 지나간 예측월을 실제가와 대조해 `snapshots/accuracy.json`
  (품목별·기간별 MAE·편향·밴드적중률, '현재가 유지' 나이브 대비)을 누적합니다.
  시각은 KST `YYYY-MM-DD HH:MM`. 목적: 과거 전망의 정확도를 계량화해 이후 전망에 반영.

## 구성

| 파일 | 역할 |
|---|---|
| `_common.py` | CSV 로더 + 야후 수집 + `merge_manual`(CSV 우선 병합) + `save_snapshot` 공용 로직 |
| `manual/*.csv` | **1차 시세 소스** (`date,price`). 품목별 CSV — `manual/README.md` 참고 |
| `prices.py` | **시세만** 갱신 (Gemini 미사용) — `history_3y`·`macro`·`prices_date` 교체 |
| `analyze.py` | 시세 + Gemini → 6개월 전망(`forecast_data`) 생성 + 전망 스냅샷 저장 |
| `raw_materials_forecast.json` | 대시보드가 읽는 단일 데이터 파일 (Actions 자동 갱신) |
| `snapshots/forecast/*.json` | 배치별 전망 동결본 (KST 시각 + 월별 base/bull/bear·타겟·변화율) |
| `snapshots/index.json` | 전 스냅샷의 6개월 타겟·현재가·변화율 요약 (변동 추적·차트용) |
| `snapshots/accuracy.json` | 지나간 예측월 vs 실제가 원장 + 집계(MAE·편향·밴드적중·나이브대비) |
| `backtest.py` + `backtest/` | (분석용·live 무관) 통계 전망 워크포워드 백테스트 — pinball·커버리지·PIT·DM검정. 방법론 튜닝 근거 |
| `index.html` + `dashboard.js` | 정적 대시보드 (다크 테마) |
| `.github/workflows/prices.yml` | 평일 06:30 KST + `manual/**` push 시 시세 갱신 |
| `.github/workflows/run.yml` | **월요일 07:00 KST 만** AI 전망 생성 (+ 수동 실행) |
| `.github/workflows/pages.yml` | GitHub Pages 배포 (push / 위 두 워크플로 완료 시) |
| `index_github.html` | 구 링크 호환용 → `index.html` 리다이렉트 |

- **↻ 새로고침** 버튼: 커밋된 JSON 파일을 다시 받아옴 (워크플로 실행 안 함).
- **↻ AI 분석 갱신** 버튼: `run.yml` 을 지금 실행 (proxy 배포 시 자동, 아니면 Actions 페이지 열기).
- `analyze.py` 는 Gemini `temperature=0.2` + 직전 실행값과 50:50 블렌드로 실행 간 급변을 억제.

## 업데이트 방법 (메-Stock 과 동일)

로컬에서 파일을 고치고 **cmd 에서 push** 하면 됩니다.

```bat
cd C:\조도희\원자재시황
git add -A
git commit -m "수정 내용"
git push
```

- `index.html` / `dashboard.js` / `raw_materials_forecast.json` 중 하나라도 바뀌어 push 되면
  `pages.yml` 이 자동으로 GitHub Pages 를 재배포합니다.
- 데이터는 매일 `run.yml`(예보) → `pages.yml`(재배포) 로 자동 갱신됩니다.
- 수동 갱신: 저장소 **Actions → Market Forecasting → Run workflow**.

## 최초 1회 설정

1. **Settings → Secrets and variables → Actions**
   - Secret `GEMINI_API_KEY` (필수)
   - Variable `GEMINI_MODEL` (선택 · 쉼표 구분 폴백 목록. 미설정 시 기본
     `gemini-flash-latest,gemini-3.6-flash,gemini-flash-lite-latest,gemini-3.1-pro-preview`.
     1.5·2.5 계열은 신규 사용자 404 라 넣지 말 것)
2. **Settings → Pages → Build and deployment → Source = `GitHub Actions`**
3. 접속: `https://doheecho.github.io/market-forecast/`

## (선택) "AI 분석 갱신" 버튼 — 메-Stock 방식

대시보드 상단 **↻ AI 분석 갱신** 버튼이 GitHub Actions(`run.yml`)를 바로 실행하게 하려면
`proxy/` 의 Cloudflare Worker 를 배포합니다.

```bat
cd C:\조도희\원자재시황\proxy
wrangler deploy
wrangler secret put GH_DISPATCH_TOKEN   REM fine-grained PAT · 이 리포 · Actions: Read and write
```

- 배포 URL(`https://market-forecast-proxy.<sub>.workers.dev`)을 `dashboard.js` 상단 `PROXY_BASE` 에 넣고 push.
- `GH_REPO` 는 `wrangler.toml [vars]` 에 이미 있음 → **토큰만** 넣으면 됨.
- `PROXY_BASE` 가 비어 있으면 버튼은 데이터 재조회만 합니다.

과거 유사국면은 `analyze.py` 가 실거래 시계열에서 현재 궤적과 상관이 높은 과거 구간을
찾아 실제 가격을 넣고, Gemini 는 그 위에 사건명·요약만 붙입니다. **비교창은 1년/6개월
두 가지**로 각각 산출되며 대시보드에서 버튼으로 전환합니다.

## 시세 CSV (manual/ — 1차 소스)

`manual/<품목>.csv` (`date,price`) 가 있으면 그 품목은 **CSV 값으로만** 차트·통계·
유사국면·전망을 만듭니다(야후 값·직전 파일값보다 항상 우선). CSV 가 없거나 유효
20행 미만인 품목만 야후 파이낸스로 폴백합니다 — 현재 폴백은 `steel`(HRC) 뿐.

- 파일명이 곧 키입니다: `copper.csv`, `Gold.csv`, `Iron ore.csv` … (대소문자·공백 무시,
  `aluminium→aluminum`, `Silicone→silicon` 오타 보정). `_common.py` 의 `META` 에 없는
  이름은 무시됩니다.
- 가격은 **표시 단위 그대로** 넣습니다(환산 안 함). 단위 표는 `manual/README.md` 참고.
- 갱신: 최신 CSV 로 덮어쓰고 `git add manual && git commit && git push`.
  → `manual/**` push 가 `prices.yml`(시세만) 을 돌려 대시보드에 바로 반영됩니다.
  **AI 전망(`run.yml`)은 이때 돌지 않습니다** — 전망은 월요일 스케줄 또는 수동 실행에서만.
- 새 품목을 처음 추가할 때 그 자리에서 전망까지 만들려면 Actions → **Market
  Forecasting → Run workflow** 를 한 번 눌러 주세요.

니켈·아연·텅스텐은 애초에 무료 자동 소스가 없어(야후=선물 없음, stooq=봇차단,
investing.com=차단, KOMIS=API 없음, LME=유료) CSV 가 유일한 경로입니다.

## 로컬 실행 / 확인

```bat
pip install -r requirements.txt
set GEMINI_API_KEY=<키>
python analyze.py
python -m http.server 8000        REM  http://localhost:8000
```

`dashboard.js` 는 같은 경로의 `raw_materials_forecast.json` 을 먼저 읽고,
실패하면 `raw.githubusercontent.com` 으로 폴백합니다 (파일 직접 열기 대비).

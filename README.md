# 원자재 시황 — 6대 핵심 원자재 AI 가격 전망

WTI·전기동·알루미늄·금·은·백금의 6개월 가격 전망(Base/Bull/Bear) 대시보드.
매일 07:00 KST 에 GitHub Actions 가 야후 파이낸스 시세를 수집하고 Gemini 로
전망 JSON 을 생성해 커밋합니다. (메-Stock 과 동일한 구성)

## 구성

| 파일 | 역할 |
|---|---|
| `analyze.py` | 시세 수집 → Gemini 전망 생성 → `raw_materials_forecast.json` 저장 |
| `raw_materials_forecast.json` | 대시보드가 읽는 데이터 (Actions 가 자동 갱신) |
| `index.html` + `dashboard.js` | 정적 대시보드 (다크 테마) |
| `.github/workflows/run.yml` | 매일 예보 생성 |
| `.github/workflows/pages.yml` | GitHub Pages 배포 (push / 예보 완료 시) |
| `index_github.html` | 구 링크 호환용 → `index.html` 리다이렉트 |

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
   - Variable `GEMINI_MODEL` (선택 · 쉼표 구분 폴백 목록. 기본
     `gemini-flash-latest,gemini-2.5-flash,gemini-flash-lite-latest`)
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

과거 유사국면은 `analyze.py` 가 실거래 시계열에서 현재 12개월과 상관이 가장 높은
과거 구간을 찾아 실제 가격을 넣고, Gemini 는 그 위에 사건명·요약만 붙입니다.

## 로컬 실행 / 확인

```bat
pip install -r requirements.txt
set GEMINI_API_KEY=<키>
python analyze.py
python -m http.server 8000        REM  http://localhost:8000
```

`dashboard.js` 는 같은 경로의 `raw_materials_forecast.json` 을 먼저 읽고,
실패하면 `raw.githubusercontent.com` 으로 폴백합니다 (파일 직접 열기 대비).

# 원자재 시황 — 핵심 원자재 AI 가격 전망

WTI·전기동·알루미늄·금·은·백금·열연강판·철광석의 6개월 가격 전망(Base/Bull/Bear) 대시보드.
(니켈·아연·텅스텐은 자동 무료 소스가 없어 `manual/` CSV 로 주입 — 아래 "니켈·아연·텅스텐" 참고)
**시세는 평일 매일**, **AI 전망은 주 1회(월요일)** GitHub Actions 가 갱신합니다.

## 구성

| 파일 | 역할 |
|---|---|
| `_common.py` | 야후 수집 + `manual/` CSV 로더 공용 로직 (analyze/prices 공유) |
| `manual/*.csv` | 야후에 없는 니켈·아연·텅스텐 시세를 수동으로 넣는 곳 (`manual/README.md` 참고) |
| `prices.py` | **시세만** 갱신 (Gemini 미사용) — `history_3y`·`macro`·`prices_date` 교체 (`manual/` 병합) |
| `analyze.py` | 시세 + Gemini → 6개월 전망(`forecast_data`) 생성 |
| `raw_materials_forecast.json` | 대시보드가 읽는 단일 데이터 파일 (Actions 자동 갱신) |
| `index.html` + `dashboard.js` | 정적 대시보드 (다크 테마) |
| `.github/workflows/prices.yml` | 평일 06:30 KST 시세 갱신 |
| `.github/workflows/run.yml` | 월요일 07:00 KST AI 전망 생성 |
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

과거 유사국면은 `analyze.py` 가 실거래 시계열에서 현재 궤적과 상관이 높은 과거 구간을
찾아 실제 가격을 넣고, Gemini 는 그 위에 사건명·요약만 붙입니다. **비교창은 1년/6개월
두 가지**로 각각 산출되며 대시보드에서 버튼으로 전환합니다.

## 니켈·아연·텅스텐 (manual/ CSV)

이 3종은 **무료로 자동 수집할 소스가 없습니다.** (야후=선물 없음, stooq=봇차단,
investing.com=Cloudflare 차단 + 과거값 오류, KOMIS=공개 API 없음, LME=유료.)
그래서 `manual/` 폴더에 CSV 를 넣으면 파이프라인이 알아서 병합합니다.

1. 엑셀에서 **날짜 + 종가** 두 열로 정리 → `CSV 로 저장`.
   (KOMIS <https://www.komis.or.kr> → 광종/국가정보 → 광종정보 에서 엑셀 받아 정리해도 됨)
2. `manual/nickel.csv`, `manual/zinc.csv`, `manual/tungsten.csv` 를 덮어쓰기.
   가격은 **표시 단위 그대로**(니켈·아연 USD/ton, 텅스텐 USD/mtu). 자세한 형식은
   `manual/README.md` 참고.
3. `git add manual && git commit && git push`
   → `prices.yml`(매일)·`run.yml`(주간)이 매 실행마다 읽어 병합하고,
   다음 배포부터 대시보드에 **니켈 / 아연 / 텅스텐 탭**이 생깁니다.

`prices.py` 는 야후 갱신 때 이 키들을 `manual/` 값으로 유지하고, `analyze.py` 는
그 시세로 6개월 전망·유사국면까지 만듭니다. 갱신은 CSV 를 새로 덮어쓰고 푸시하면 됩니다.

## 로컬 실행 / 확인

```bat
pip install -r requirements.txt
set GEMINI_API_KEY=<키>
python analyze.py
python -m http.server 8000        REM  http://localhost:8000
```

`dashboard.js` 는 같은 경로의 `raw_materials_forecast.json` 을 먼저 읽고,
실패하면 `raw.githubusercontent.com` 으로 폴백합니다 (파일 직접 열기 대비).

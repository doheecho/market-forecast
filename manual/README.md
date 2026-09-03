# manual/ — 시세 CSV (1차 소스)

이 폴더의 `<품목>.csv` 가 **모든 품목의 기본 시세 소스**입니다.
`prices.py`·`analyze.py` 가 매 실행마다 읽어, CSV 가 있는 품목은 그 값으로만
차트·통계·유사국면·6개월 전망을 만듭니다(야후·직전 파일값보다 항상 우선).
CSV 가 없거나 유효 20행 미만인 품목만 야후 파이낸스로 폴백합니다(현재 `steel` 뿐).

파일명이 곧 키입니다: `copper.csv`, `Gold.csv`, `Iron ore.csv` …
(대소문자·공백 무시, `aluminium→aluminum`·`Silicone→silicon` 보정.
`_common.py` 의 `META` 에 없는 이름은 무시됩니다.)

## 넣는 법 (제일 쉬운 방법: GitHub 웹에서 바로)

1. 엑셀에서 **날짜 + 종가** 두 열로 정리 → `다른 이름으로 저장 → CSV`.
2. GitHub 저장소 → `manual/` 폴더 → 해당 파일 클릭 → 연필 아이콘(Edit)
   → 내용 전체 지우고 새 CSV 붙여넣기 → **Commit changes**.
   (또는 폴더에서 **Add file → Upload files** 로 파일을 끌어다 놓고 커밋)
3. 커밋하면 **`prices.yml`(시세만) 워크플로가 자동 실행**됩니다(`manual/**` push 트리거).
   2~3분 뒤 대시보드에 반영됩니다. **AI 전망(`run.yml`)은 이때 돌지 않습니다** —
   전망은 매주 월요일 스케줄 또는 Actions 수동 실행에서만 갱신됩니다.
   새 품목을 처음 넣고 전망까지 바로 만들려면 Actions → **Market Forecasting →
   Run workflow** 를 한 번 눌러 주세요.

로컬에서 고칠 경우엔 `git add manual && git commit && git push`.

## CSV 형식

- 첫 줄은 헤더. 열 이름은 자유 — 아래 단어가 들어가면 자동 인식:
  - 날짜 열: `date`, `일자`, `기준일`, `날짜`, `거래일` …
  - 가격 열: `price`, `close`, `종가`, `가격`, `USD`, `평균` …
- 날짜: `2026-08-28`, `2026.08.28`, `2026/8/28`, `20260828`, `2026-08`(월만) 모두 허용.
- 가격: 숫자(쉼표·통화기호 섞여도 됨). **표시 단위 그대로** 넣으세요(아래 단위 표 참고).
  환산 안 합니다.
- 인코딩: UTF-8 / CP949(euc-kr) 자동 판별. (KOMIS 엑셀→CSV 는 보통 CP949)
- 유효 행이 20개 미만이면 그 품목은 무시되고 야후 폴백으로 넘어갑니다
  (헤더만 있는 빈 템플릿은 조용히 넘어감).

## 예시

```csv
date,price
2021-01-04,16250.00
2021-01-05,16380.00
...
2026-08-28,15120.00
```

기간은 길수록 좋습니다(과거 유사국면 분석에 최소 30개월 필요). 6년치면 충분합니다.
갱신은 최신 CSV 로 덮어쓰고 다시 푸시하면 됩니다(주 1회면 충분 — 전망도 주 1회라).

## 각 원자재 단위
- 니켈 : USD/ton, LME 현물
- 아연 : USD/ton, LME 현물
- 텅스텐 : RMB/mt, Oxide WO3 99.95% 중국 현물
- 알루미늄 : USD/ton, LME 현물
- 전기동 : USD/ton, LME 현물
- 실리콘 : USD/ton, Ferro 75% 중국(FOB) 현물
- 금 : USD/ozt, LBMA 현물
- 은 : US￠/ozt, LBMA 현물
- 백금 : USD/ozt, LPPM 현물
- WTI : USD/bbl, NYMEX Futures
- H형강 소형/중형 : KRW/ton, 한국(1차 유통가)
- 냉연코일 : USD/ton, MEPS 현물
- 고철 중량A : KRW/ton, 한국(도매가)
- 고철 생철 : KRW/ton, 한국(도매가)
- 선재 : USD/ton, MEPS 현물
- STS 304 : KRW/ton, CR 2mm 한국(도매가)

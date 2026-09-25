# CLAUDE.md — 낙찰가 예측 도우미 (bid-predictor)

나라장터 공사 입찰의 과거 낙찰 데이터(사정율 분포)로 다음 입찰의 예상 사정율·투찰금액을 추정하는 정적 PWA.
GitHub Pages(main 브랜치 루트)로 배포하고, 공공 데이터는 GitHub Actions 가 매일 `data/` JSON 으로 갱신한다.
개인 데이터(관심공고)는 1단계에서 localStorage, 2단계에서 Firebase(Firestore)로 옮길 예정.

## 작업 규칙
- **요청한 부분만 수정**한다. 관련 없는 리팩터링·디자인 변경 금지.
- **하단 탭 5개 유지**: 입찰공고 / 예측분석 / 관심공고 / 통계 / 설정 (PC에서는 좌측 사이드바).
- **앱이 쓰는 파일**(index.html, app.js, style.css, manifest.json, icons/)을 바꾸면 **sw.js 의 `VERSION` 을 올린다.**
  예외: reference/, scripts/, .github/ 워크플로, data/ 는 버전 올릴 필요 없음.
- **디자인 유지**: 흰 배경, 파랑 #2F6FED, 카드형, Noto Sans KR, 다크모드(`prefers-color-scheme` + `data-theme`).
- **data JSON 형식을 바꾸면 app.js 와 scripts/collect.py 를 함께 수정**하고 아래 구조 설명도 고친다.
- **비밀값(API 키 등)은 코드에 넣지 않는다.** GitHub 시크릿(`DATA_GO_KR_KEY`)만 사용.
- 모든 확률·예측 옆에 표본 수 표시, 30건 미만이면 "참고 부족". 예측 화면에 "참고용이며 낙찰을 보장하지 않음" 문구 유지.
- 관심공고 저장은 app.js 의 `WatchStore`(list/save/remove, 모두 async)만 통한다 — 2단계에서 이것만 Firestore 로 교체.
- **main 에 직접 푸시**한다 (PR 불필요).

## 파일 지도
```
index.html            화면 뼈대 (5개 section + nav)
app.js                앱 로직 전부 (데이터 로딩, 예측, 차트, 탭별 렌더)
style.css             스타일 (색 토큰은 :root, 다크모드 재정의 포함)
sw.js                 서비스워커 — VERSION, 앱 셸 캐시, data 캐시(?v=갱신시각 단위)
manifest.json         PWA 매니페스트
icons/                아이콘 (svg, 192/512 png)
.nojekyll             _sample_*.json 등 밑줄 파일도 Pages 에 게시되도록
scripts/collect.py    수집기 (Actions 에서 실행)
scripts/commit_data.sh  data/ 변경 커밋·푸시 (워크플로에서 단계마다 호출)
scripts/model.py      전국 추천 모델·역검증 → data/model.json (수집 단계마다 실행, numpy)
scripts/korea.py      시도·시군구 파싱, 면허 23개 + 옛 명칭 별칭표
scripts/regions.json  개찰 상세(전체 순위·복수예가)를 수집할 시·도 목록. 예: ["강원"]
.github/workflows/collect.yml  02:00 KST 전체 + 09·13·17시 공고만 + 수동 실행(start_date, reset_backfill, quick_only)
reference/prototype.html       초기 프로토타입 (디자인 참고용, 가상 데이터 코드는 쓰지 않음)
data/                 수집 결과 (아래)
```

## 수집 (scripts/collect.py)
- API: 공공데이터포털 조달청 나라장터 `낙찰정보서비스`(ScsbidInfoService), `입찰공고정보서비스`(BidPublicInfoService).
  엔드포인트 후보는 `SERVICES`, 오퍼레이션은 `OPS`, 필드 이름 후보는 `F_*` 목록. 첫 성공 응답 1건을 `data/_sample_{op}.json` 에 저장하므로 필드명이 다르면 그걸 보고 `F_*` 를 고친다.
- 단계(이름): ① `공고` 진행중 공고(최근 공고·기초금액·면허제한·참가가능지역) ② `최근낙찰` 최근 40일 낙찰 목록 ③ `과거낙찰` 과거 낙찰 목록을 한 달씩 과거로, 최근 24개월(`RECENT_FIRST_MONTHS`)까지 먼저 ④ `상세` regions.json 지역의 개찰 전체 순위·복수예가(공고 1건당 2회 이상 호출, 최신 개찰부터; 첫 상세는 `DETAIL_RESERVE` 250회를 과거 수집 몫으로 남김) ⑤ `과거낙찰` 24개월까지 ⑥ `상세` 남은 한도 ⑦ `과거낙찰` 나머지(기본 3년, `meta.backfill.cursor`). 실제 순서는 ①②④⑤ `지역보강` ⑥⑦ (상세를 과거 수집 앞으로).
- `지역보강`: 과거 낙찰 레코드에 참가가능지역(`rgn`)을 채운다. 공고 게시 달 단위로 최신→가장 오래된 낙찰 달까지 한 번(`meta.rgn_fill` {cursor: YYYYMM, done}), 입찰공고 호출 `REGION_RESERVE`(250)회는 남김. 과거낙찰 단계도 달마다 참가가능지역을 같이 받는다. 최근낙찰은 notice_cache 의 rgn 으로 보강.
- 환경변수 `STEPS` 로 단계를 골라 실행. 워크플로는 1차 `공고,최근낙찰`(MAX_MINUTES 40) → 커밋 → 2차 `과거낙찰,지역보강,상세` → 커밋 순서라 공고는 몇 분 안에 앱에 뜬다. 09·13·17시 예약 실행과 `quick_only` 수동 실행은 1차만.
- 하루 호출 한도: 서비스별 `API_DAILY_LIMIT`(기본 950). 호출 수는 `meta.api.calls` 에 날짜별로 기록, 넘으면 저장 후 다음 날 이어서.
- 예정가격이 없으면 `낙찰금액 ÷ 낙찰률` 로 역산, 사정율 `sr = 예정가격 ÷ 기초금액 × 100` (80~120 벗어나면 버림).
- 공고번호-차수(`id`)로 중복 병합. 워크플로는 meta.json 외 파일이 바뀐 경우에만 커밋.
- 파일 하나가 45MB 를 넘으면 `이름_2.json`, `이름_3.json` … 으로 나누고, 목록은 `meta.files` 에 기록(앱은 이 목록만 읽음).

## data JSON 구조
모든 파일은 공백 없는 JSON. 없는 값은 키 자체를 생략.

### data/meta.json
```jsonc
{
  "updated_at": "2026-09-26T02:05:11+09:00",   // 데이터가 마지막으로 바뀐 시각 (앱 캐시 키 ?v= 로 사용)
  "v": 1,
  "counts": {"bids": 1234, "scsbid": {"강원": 9000, ...}, "scsbid_total": 150000, "opening": {"강원": 3000}},
  "files": {
    "scsbid": {"강원": ["scsbid/강원.json"], "경기": ["scsbid/경기.json", "scsbid/경기_2.json"]},
    "opening": {"강원": {"2025": ["opening/강원/2025.json"], "2026": ["opening/강원/2026.json"]}}
  },
  "backfill": {"target_start": "20230926", "cursor": "20250831", "oldest": "20250901", "done": false, "months_done": 13},
  "detail": {"regions": ["강원"], "강원": {"total": 9000, "done": 3000, "failed": 2},
             "remaining": 6000, "per_day": 430, "eta_days": 14, "history": [430, 425]},
  "rgn_fill": {"cursor": "202601", "done": false},
  "api": {"base": {"scsbid": "https://…", "bid": "https://…"}, "calls": {"date": "20260926", "scsbid": 950, "bid": 40}},
  "notice_last": "2026-09-26T02:00+09:00",
  "last_run": {"at": "…", "minutes": 42.1, "calls": {…}, "errors": ["…"]}
}
```

### data/bids.json — 진행중 공고 (마감 전)
`{"v":1, "items":[ … ]}` 마감 임박 순. 항목:
| 키 | 뜻 |
|---|---|
| id | 공고번호-차수 (예: `R26BK01234567-000`) |
| no, ord | 공고번호, 차수 |
| nm | 공고명 |
| org, dmd | 공고(발주)기관, 수요기관 |
| sido, sgg | 시도 약칭(서울…제주), 시·군·구 |
| lic | 면허 목록(23개 정식 이름) |
| rgn | 참가가능지역 원문 목록 |
| base, est | 기초금액, 추정가격 (원) |
| floor | 낙찰하한율(%) |
| a | A값(원) = 국민연금+건강보험+노인장기요양+퇴직급여+산업안전보건관리비+안전관리비+품질관리비 |
| net | 순공사원가(원) |
| rng | 예가범위 [하한%, 상한%] 예: [-2, 2] |
| ntce, close, open | 공고일시, 입찰마감일시, 개찰일시 (`YYYY-MM-DD HH:MM`, KST) |
| url | 나라장터 상세 링크 |
| seen | 수집기가 처음 본 시각(ISO) — 앱의 NEW 표시 기준 |

### data/model.json — 전국 추천 모델 + 매일 역검증 (scripts/model.py)
```jsonc
{"v":1, "updated_at":"…", "data":{"from","to","rows"}, "grid":{"x0":97,"step":0.01,"n":601,"scale":1e5},
 "band":1.49, "smooth":0.2, "amt_edges":[7.7,8,8.3,8.6,9,9.5], "meanS":{"-3,3":99.857,"-2,2":99.996},
 "npred":{"g":로그평균, "sd", "shrink":5, "t":{"a":{"금액대|예가범위":[로그평균,건수]}, "sa":{"시도|금액대"}, "saf":{"시도|금액대|하한율"}, "l":{"면허"}, "sl":{"시도|면허"}, "o":{"기관"}},
          "adj":{"k":10, "t":{"hl":{"시도|시군구|면허":[잔차평균,건수]}, "r":{"범위":…}, "rs":{"범위|시도|금액대":…}}}},
 "curves":{"-3,3":[{"k":round(10·ln 참가수)(2간격), "x":추천, "p":낙찰확률, "lo","hi":안전범위, "n":표본, "r":평균 1/참가수,
                   "cnt":참가 중앙값, "pk":[[x,p]×3], "v":[그림 범위], "c":[601칸 ×1e5 정수]}], "-2,2":[…]},
 "validation":{"months":[{"m","n","near","mean","rand","below_near","below_mean"}], "segments":[{"k":[하한,상한],"n","near","mean","rand"}],
               "total":{"n","near","mean","rand","near_ci","mean_ci","below_near","below_mean"}, "rule"}}
```
- 참가수 예측(로그): a → sa → saf → l → sl → o 순으로 `est = (n·평균 + 5·est)/(n+5)`, 그다음 보정층 hl → r → rs 를 `est += n·잔차평균/(n+10)` (3건 미만 키는 뺌). 앱 `predictLnN` 과 키 형식이 같아야 한다(금액대 = log10(기초금액) ≥ 경계 개수, 하한율 `%g`, 면허 정렬 후 `+`). 2026-09 공고 1,647건에서 앱·Python 결과 일치 확인.
- 범위(`rgn_scope` / 앱 `rgnScope`): rgn 없음 n, 시·군 하나 g, 시·군 2~4 G, 시·도 하나 s, 시·도 여럿 S (시·도 이름만 있는 항목이 섞이면 s/S). 시·도는 원문 첫 단어, 시·군·구는 둘째 단어가 `…[시군구]` 일 때. 실시간 검색 공고는 rgn 이 없어 bids.json 의 같은 공고 rgn 을 쓴다(`rgnOf`).
- 보정층 근거: hl 은 표본외 참가수 예측 상관 0.66 → 0.74, 예측 참가 하위 20% 공고의 실제 평균 1/참가수 6.4% → 8.0%(공고 고르기 효과). 범위는 2026-09 교차검증 +0.03~0.05 — 지역보강이 과거 레코드를 채울수록 효과가 생긴다(채우기 전엔 r 표가 n 하나).
- 곡선: 예가범위 × 예상 참가수 c 마다, 최근 24개월 전국 공고 중 **예측** 참가수(학습 행에도 같은 참가수 예측표 적용)가 로그 ±0.4(c×0.67~×1.49) 안인 공고(300건 미만이면 가까운 300건)로 승리 구간을 쌓아 ±0.2%p. 앱은 예상 참가수에 가장 가까운 k 를 쓴다.
- 역검증: 최근 12개월 각 달을 그 이전 24개월로만 추천(예측 참가수 → 곡선)해 S ≤ x < W 를 센다. 비교 = 평균 사정율, 평균 업체(1/참가수).
- 근거(2026-09 재검토, 2025-07~2026-09 중 11개월 60,534건 표본외): 실제 참가수로 곡선을 고르면(쓸 땐 예측 참가수) ×0.90 — 평균 업체보다 못함. 예측 참가수로 학습 + ±0.2%p → ×1.00 (11개월 중 10개월 개선, model.json 역검증 1,183 → 1,355건). ±0.01 은 우연한 봉우리라 새 달에서 못 이김. 참가수를 정확히 알아도 상한 ×1.12 — 위치 선택만으로 큰 우위는 없고, 예상 150곳↑ 에서만 ×1.2~1.4. 참가가능지역 범위(시·도 전체 +0.35, 시·군 제한 −0.39 로그)를 넣으면 참가수 예측 상관 0.69 → 0.73 이지만 과거 낙찰 레코드에 참가가능지역이 없어 아직 못 씀.
- 앱 확률 표시(`valWinP`): 곡선 최대값 e.p 는 과거에 맞춘 값이라 부풀려짐 → 예상 낙찰확률 = e.r(비슷한 공고 1/참가수 평균) × 역검증 구간 배수(near/rand). 목록 정렬·기대 수주액도 이 값.

### data/lic_map.json — 공고별 면허제한·참가가능지역 (수집기 `write_lic_map`, 최근 60일 공사)
`{"v":1, "lic":[면허명…], "items":{"공고ID":[lic 번호…]}, "rg":[지역 원문…], "rgn":{"공고ID":[rg 번호…]}}` — 실시간 검색의 업종 거르기 + "참가 가능한 공고만" 판정용.
실시간 공고의 `lic` 는 주공종·부대공종이라 면허제한이 아니다 → `eligibility` 는 `licOf`(lic_map → `b.reqLic`, 없으면 '확인 필요')·`rgnOf`(b.rgn → lic_map → bids.json)로 판정.
lic_map 에 없는 실시간 공사 공고는 `fetchLiveLimits`(renderLive 뒤 10건씩 반복)가 `getBidPblancListInfoLicenseLimit`·`…PrtcptPsblRgn`(inqryDiv=2, 공고번호)으로 바로 조회 → `b.reqLic`, `b.rgn`([] = 지역 제한 없음), `b.limOk`(면허 제한 없음이면 참가 가능). 같은 함수가 기초금액 없는 카드는 bids.json 값 → 없으면 `enrichLive` 로 기초금액·A값·예가범위를 채워 목록에서 바로 추천 투찰가를 보여 준다.
실시간 공고 `corr`(ntceKindNm 정정)·`sui`(cntrctCnclsMthdNm 수의) → 카드 태그. 면허제한은 대업종(4991·4992 등) 단위로 온다(2026-09 최근 60일 확인) — 주력분야는 적격심사 실적 평가용. 카드에는 `licTags` = "요구 면허" + 우리 면허는 ✓. 모델 참가수 예측도 실시간 공고는 licOf 를 쓴다.
한계: 면허제한 그룹(lmtGrpNo, 그룹 안은 모두 필요·그룹끼리는 택일)을 구분하지 않고 하나라도 겹치면 가능으로 본다.

### data/scsbid/{시도}.json — 과거 낙찰 (최근 개찰 순)
`{"sido":"강원", "v":1, "part":1, "parts":1, "items":[ … ]}` 항목: `id, no, ord, nm, org, dmd, sido, sgg, lic, rgn, base, a, net, floor, rng` (bids 와 같은 뜻, rgn 은 지역보강 전이면 없음) +
| 키 | 뜻 |
|---|---|
| plan | 예정가격(원) — 없으면 낙찰금액÷낙찰률로 역산 |
| amt | 낙찰금액(원) |
| rate | 낙찰율(%) = 낙찰금액 ÷ 예정가격 |
| cnt | 참가업체 수 |
| date | 개찰일 `YYYY-MM-DD` |
| win, winBiz | 낙찰업체명, 사업자번호(숫자만) |
| sr | 사정율(%) = 예정가격 ÷ 기초금액 × 100 |

시도를 알 수 없는 레코드는 `scsbid/기타.json`.

### data/opening/{시도}/{연도}.json — 개찰 전체 순위 + 복수예가 (regions.json 지역만)
```jsonc
{"sido":"강원", "year":"2026", "part":1, "parts":1,
 "corps": [["업체명","사업자번호"], …],            // 이 파일 안에서만 쓰는 업체 표
 "bids": {"공고ID": {"date":"2026-09-24", "plan":184557153, "base":187000000,
                    "p": [[번호1~15, 예비가격, 추첨여부0/1, 추첨횟수], …],
                    "r": [[순위, corps인덱스, 투찰금액, 투찰률(%), "비고"(있을 때만)], …]}}}
```
`data/opening/{시도}/index.json`: `{"done": {공고ID: 연도}, "fail": {공고ID: 실패횟수}}` (수집기 전용).

### data/notice_cache.json (수집기 전용, 앱은 읽지 않음)
최근 60일 공고 정보. 낙찰 레코드 보강용. `{"items": {id: {…bids 항목 + lic_list, lic_raw, site, cancel, old}}}`

### data/_sample_{op}.json
각 API 첫 응답 1건 원본 + 필드 목록. 필드명 확인용.

## 앱 동작 메모
- 앱은 meta.json → 사용자가 고른 시·도의 파일만 불러옴(메모리 + 서비스워커 `data-v1` 캐시, URL 에 `?v=updated_at`).
- NEW: `localStorage bp.lastVisit` 이후 `seen` 인 공고. 탭 배지: `bp.bidsSeenAt` 이후 수.
- 투찰금액 = (예정가격 − A값) × 낙찰하한율 + A값 (원 단위 올림). 낙찰하한율 기본 87.745.
- 투찰 사정률 x = 투찰금액을 위 식으로 되돌린 값(`bidToSr`). x ≥ 실제 사정율 S ⟺ 투찰금액 ≥ 낙찰하한가.
- **승리 구간**(`winWindow`): 과거 공고마다 [S, W), W = 실제 낙찰자 투찰 사정률(낙찰금액으로 계산). 개찰 상세가 있으면 정확한 예정가격과 "x ≥ S 인 투찰 중 최소"를 W 로(적격심사 탈락 보정). 유효: 0 ≤ W−S < 1.
- **낙찰확률 곡선**(`winCurve`): 97~103% 를 0.001 간격으로, 승리 구간을 차분 배열로 쌓아 누적합 ÷ 가중치 합. 가중치 exp(−경과개월/12), 기준 시·도 ×2. ±`curveSmooth`(기본 0.01%p, 백테스트가 0.005/0.01/0.03/0.05 중 골라 `localStorage bp.curveSmooth` 에 저장) 이동평균의 최대점 = 추천 x*. 상위 3 봉우리, 안전 범위(최대의 90% 이상 구간), 무작위 기준(가중 평균 1/참가수) 함께 반환.
- 예측분석 화면: 사용법 카드(`#pGuide`, 한 번 닫으면 `bp.guideClosed`) → ① 지역 → ② 이번 공고 정보(기초금액·예가범위 `#pRng`·A값·하한율·예상 참가 수, 입력하면 계산기 칸도 같이 채움) → 예측 실행. 시·군·면허·체크박스는 "고급: 참고 분포 조건"(추천 금액에 영향 없음)으로 접어 둠.
- 곡선 표본은 넓게: 지역 최근 24개월 + 예가범위 같음(30건↑). 면허·금액으로 쪼개지 않는다(우연한 봉우리). 면허·금액 등 필터는 참고용 평균 분포에만.
- 예상 참가업체 수(`expectedCnt`): 같은 발주기관 → 같은 면허·금액 0.5~2배 → 지역 전체 순으로 5건 이상인 첫 단계의 cnt 중앙값. 공고별 예상 낙찰확률 = 곡선값 × (곡선 표본 cnt 중앙값 ÷ 예상 참가 수). 기대 수주액 = 예상 낙찰확률 × 기초금액(없으면 추정가격).
- **백테스트**(설정): 롤링 표본외 — 최근 N개월 각 달을, 그 이전 24개월만으로 예가범위별 x* 를 정해 시험. 무작위(1/참가수)·평균·곡선(폭 4가지) 비교, Wilson 95% 신뢰구간, 무작위 대비 배수. 채택 = 시험 2,000건↑ 그리고 배수 신뢰구간 하한 > 1. 결과는 `bp.btResult[시도]` 에 저장해 예측 화면 배지로 표시.
- **내 투찰 기록**(관심공고): WatchStore 항목에 `myBid`(실제 넣은 금액). 개찰 뒤 `judgeBid` 로 낙찰권/하한 미달/1위보다 높음 + 차이 금액 + (상세 있으면) 예상 순위. `calibrate` = 내 기록 전체에서 투찰 사정률을 −1~+1%p 옮겼을 때 낙찰권이 가장 많았던 이동량 → `bp.myCal` 에 저장해 예측 화면 팁에 표시.
- 입찰공고 탭은 두 모드: **실시간 검색**(조달청 `BidPublicInfoService` 의 `getBidPblancListInfo{Cnstwk|Servc|Thng|Frgcpt|Etc}PPSSrch` 를 브라우저에서 직접 호출, CORS 허용됨. 업무구분 공사·용역·물품·외자·기타, `LIVE_KINDS`. 검색조건 조회가 키 오류 외 이유로 실패하면 기본 목록 조회 + 앱에서 거르기로 대체. 예측은 공사만) / **진행중 공고**(자동 수집 bids.json). 서비스키는 설정 탭에서 입력해 `localStorage bp.apiKey` 에만 저장(저장소에 넣지 않음). 다른 기기로는 설정 탭 "폰으로 보내기 (QR)" → `앱주소#key=…` 링크를 열면 init() 이 저장하고 주소에서 지운다(# 뒤라 서버로 안 감). 키가 있으면 실시간이 기본.
  조달청 API 는 한 번에 약 1개월까지만 조회되므로 기간을 30일 구간(`liveWindows`, 최신부터)으로 나눠 차례로 받고, 07/범위 오류면 구간을 반으로 나눠 다시. 시·군·참가 가능처럼 앱에서 거르는 조건이 있으면 맞는 공고가 30건 모일 때까지(최대 12번 호출) 자동으로 더 받는다.
  업종은 조달청에 보내지 않는다: PPSSrch 의 업종 조건(indstrytyNm·indstrytyCd)은 맞는 공고가 있어도 0건을 돌려준다(실사용 확인, 다른 개발자도 같은 보고). 대신 받은 공고의 주공종·부대공종(mainCnsttyNm, subsiCnsttyNm1~9) + 수집된 면허제한 `data/lic_map.json` 으로 앱에서 거르고(`liveHasLic`), 이때는 999건씩 받는다. `LIC_CODES` 는 참고용 업종코드(금속창호 4991, 도장·습식·방수·석공 4992 …).
  설정 "조달청 대조 점검"(`runVerify`): 수집된 최근 1개월 낙찰 10건을 조달청에 다시 조회해 낙찰금액·예정가격·참가수를 비교.
  실시간 결과는 같은 공고번호의 마지막 차수만, 취소공고 제외. "이 공고로 예측" 때 `getBidPblancListInfoCnstwkBsisAmount`(inqryDiv=2, 공고번호)로 기초금액·A값·예가범위를 채운다(`enrichLive`). 검색 조건 파라미터 이름(bidNtceNm, ntceInsttNm, dminsttNm, prtcptLmtRgnNm, indstrytyNm, presmptPrceBgn/End, bidClseExcpYn)은 실제 키로 확인 전 — 안 먹히면 여기부터 확인.
- **추천은 전국 모델 우선**(`modelPredict` → `recFromModel`): 공고의 예상 참가수(`predictLnN`, 입력값이 있으면 그 값)와 예가범위(모르면 ±3%)로 `model.curves` 에서 곡선을 골라 추천 x·안전 범위·낙찰확률. 모델이 없으면 지역 곡선(`recFromLocal`). 예측 화면 파란 카드에 "✅ 추천 체크"(경쟁 규모·추천 위치·금액·입력 누락·순공사원가·기록), 배지 = 역검증 요약(`valBadge`). 설정 탭 "역검증" = `renderValidation`.
- **우리 업체**(설정, `Company` → `localStorage bp.company` {sido, sgg, lics[], biz}): `eligibility(b)` = 참가가능지역(rgn, 비면 제한 없음; "강원특별자치도"는 시·도 전체, "… 춘천시"는 그 시·군만) + 면허(겹치면 가능, 공고 면허 정보 없으면 '확인 필요'). 입찰공고 두 모드에 "참가 가능한 공고만"(`bElig`/`lElig`), 카드 태그 `eligTag`.
- **개찰 결과 실시간 조회**(관심공고): 개찰 시각이 지난 공고는 `fetchOpeningResult` 로 낙찰정보서비스 `getOpengResultListInfoOpengCompt`(순위) + `…CnstwkPreparPcDetail`(예정가격)을 브라우저에서 직접 조회(한 번에 5건, 결과 없으면 1시간 뒤). 사업자번호가 맞는 행 = 우리 순위·투찰금액(myBid 자동). 결과는 WatchStore 항목 `res` {n, plan, base, win, mine, top(10), xs(전체 금액)}.
- **참여한 공고**(관심공고 탭 안 전환 `watchMode` 'watch'|'joined', 하단 탭 5개 유지): 참여 = WatchStore 항목의 `joined`·`myBid`·`res.mine`. 사업자번호가 있으면 `findMyBidsInOpening` 이 수집된 개찰 상세(regions.json 지역)에서 우리 행을 찾아 자동 목록(저장 전 가상 항목, "＋ 저장"). `addJoinedByNo` = 공고번호로 추가 후 바로 개찰 결과 조회. 조달청 API에는 사업자번호로 공고를 찾는 기능이 없다.
- 금액 입력칸은 `input.money`(text) — 입력 중 쉼표, 읽기 `numOf(el)`, 쓰기 `setMoney(el, v)`.
- 예측분석 "참고 분포 조건"(시·군·면허·체크박스)은 추천 금액과 무관: 참고 사정율 분포·근거 공고·곡선의 옅은 막대·계산기 안내에만 쓰인다. 발주기관 예가 구간확률·시뮬레이터는 "예가 분석 도구"로 접어 둠.
- 공고 목록 간단 예측(`quickPredict`): 모델이 있으면 지역 파일 없이도 추천·예상 참가·낙찰확률(정렬에 사용). 없으면 `quickPredictLocal`: 같은 시도 최근 24개월 → 면허 겹침(10건 이상일 때) → 예가범위 같음(30건 이상일 때). 조건별로 결과 캐시.
- 경고: 투찰금액 < 낙찰하한가, 또는 < 순공사원가 × 98% → 빨간 경고.

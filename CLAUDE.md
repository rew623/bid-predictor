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
scripts/korea.py      시도·시군구 파싱, 면허 23개 + 옛 명칭 별칭표
scripts/regions.json  개찰 상세(전체 순위·복수예가)를 수집할 시·도 목록. 예: ["강원"]
.github/workflows/collect.yml  매일 02:00 KST + 수동 실행(start_date, reset_backfill)
reference/prototype.html       초기 프로토타입 (디자인 참고용, 가상 데이터 코드는 쓰지 않음)
data/                 수집 결과 (아래)
```

## 수집 (scripts/collect.py)
- API: 공공데이터포털 조달청 나라장터 `낙찰정보서비스`(ScsbidInfoService), `입찰공고정보서비스`(BidPublicInfoService).
  엔드포인트 후보는 `SERVICES`, 오퍼레이션은 `OPS`, 필드 이름 후보는 `F_*` 목록. 첫 성공 응답 1건을 `data/_sample_{op}.json` 에 저장하므로 필드명이 다르면 그걸 보고 `F_*` 를 고친다.
- 단계: ① 진행중 공고(최근 공고·기초금액·면허제한·참가가능지역) ② 최근 40일 낙찰 목록 ③ 과거 낙찰 목록을 한 달씩 과거로(기본 3년, `meta.backfill.cursor`) ④ regions.json 지역의 개찰 전체 순위·복수예가(공고 1건당 2회 이상 호출, 최신 개찰부터).
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

### data/scsbid/{시도}.json — 과거 낙찰 (최근 개찰 순)
`{"sido":"강원", "v":1, "part":1, "parts":1, "items":[ … ]}` 항목: `id, no, ord, nm, org, dmd, sido, sgg, lic, base, a, net, floor, rng` (bids 와 같은 뜻) +
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
- 경고: 투찰금액 < 낙찰하한가, 또는 < 순공사원가 × 98% → 빨간 경고.

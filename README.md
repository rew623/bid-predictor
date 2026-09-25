# 낙찰가 예측 도우미 (bid-predictor)

나라장터 공사 입찰의 과거 낙찰 데이터 사정율 분포를 분석해 다음 입찰의 예상 사정율·투찰금액을 추정하는 PWA입니다.
**참고용이며 낙찰을 보장하지 않습니다.**

- 앱: GitHub Pages (main 브랜치 루트)
- 데이터: GitHub Actions `데이터 수집` 워크플로가 매일 02:00(KST)에 `data/` 를 갱신

## 처음 설정
1. 공공데이터포털에서 `조달청_나라장터 낙찰정보서비스`, `조달청_나라장터 입찰공고정보서비스` 활용 신청
2. 저장소 Settings → Secrets and variables → Actions → `DATA_GO_KR_KEY` 에 서비스키(Decoding 키 권장) 등록
3. Settings → Pages → Branch: `main` / `/ (root)`
4. Actions → `데이터 수집` → Run workflow (start_date 에 `YYYYMMDD` 를 넣으면 그 날짜까지 과거 수집)

개발 규칙과 데이터 구조는 [CLAUDE.md](CLAUDE.md) 참고.

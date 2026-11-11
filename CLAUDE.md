# CLAUDE.md — 로또 번호 분석 (lotto-analyzer)

정적 PWA(GitHub Pages, main 루트). 데이터는 GitHub Actions 가 매주 `data/` 로 갱신. 같은 사용자의 낙찰 앱(rew623/bid-predictor)과 같은 방식.

## 규칙
- 매 회 독립 추첨: 어떤 분석법도 확률을 못 바꾼다. 새 분석법을 넣으면 `scripts/update.py` 역검증(METHODS)에도 넣고 무작위와 95% 구간으로 비교해 그대로 보인다. 효과가 없으면 없다고 쓴다.
- '당첨 보장'·'적중률 높은 번호' 같은 문구 금지. '참고·재미용이며 당첨을 보장하지 않음' 유지.
- 앱 파일(index.html·app.js·style.css·manifest.json·icons/)을 바꾸면 `sw.js` 의 VERSION 을 올린다.
- 디자인: 낙찰 앱과 같은 흰 배경·파랑 #2F6FED·카드형·Noto Sans KR·다크모드, 이모지 표시 유지(가시성 우선), 공 색은 동행복권 색.
- data 형식을 바꾸면 app.js 와 update.py 를 함께 고친다.
- main 에 직접 푸시.

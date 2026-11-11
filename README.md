# 로또 번호 분석

로또 6/45 1회부터 최신 회차까지 당첨번호로 시중 분석법(빈도·핫/콜드·미출현·홀짝·고저·합계·연속·AC·끝수·소수·이월·동반 출현)을 보여 주고,
각 방법을 **표본외 역검증**(매 회차 직전까지 기록만 보고 골랐다면 몇 개 맞았나)해 무작위와 비교하는 정적 PWA.

- 앱: `index.html` · `app.js` · `style.css` · `sw.js`(VERSION) · `manifest.json` · `icons/`
- 데이터: `data/draws.json`(전체 회차) · `data/analysis.json`(분석·역검증) — `scripts/update.py` 가 만든다
- 갱신: `.github/workflows/update.yml` (토 22:10·일 08:10 KST, 수동 실행 가능)
- 출처: 동행복권 공식 API(열릴 때) → 공개 미러 smok95/lotto

로또는 매 회 독립 추첨이라 어떤 분석도 당첨 확률을 바꾸지 못한다. 참고·재미용이며 당첨을 보장하지 않는다.

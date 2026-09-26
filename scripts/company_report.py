#!/usr/bin/env python3
"""우리 업체 전용 역검증 보고서 (로컬 전용 — private/ 의 회사 자료를 읽어 private/report.html 에 쓴다. 저장소에 올리지 않음)

1) 우리 지역 × 우리 면허 공사 전부: 매달 그 이전 24개월만으로 학습한 추천값(model.py 와 같은 계산)으로 넣었다면 몇 건 낙찰됐나.
   비교: 평균 사정율(사람들이 흔히 쏘는 값), 회사 실제 투찰률 분포(더비스식), 공정 기대 Σ1/(참가+1).
2) 회사 실제 투찰 이력(더비스 엑셀, private/*.xls): 공사를 수집 낙찰과 개찰일+기초금액으로 맞춰, 우리 추천값이었다면.

설정: private/company.json {"sido": "강원", "lics": ["금속창호·지붕건축물조성", ...]}
실행: python scripts/company_report.py   (작업 스케줄러가 매일 아침 git pull 뒤 실행)
"""
import collections
import datetime as dt
import glob
import html
import json
import math
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model as M  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA, PRIV = ROOT / "data", ROOT / "private"
CONSTR = ('(대)', '건축', '토건', '토목', '조경', '전기', '통신', '소방', '실내건축', '기계가스', '상하수도', '철콘', '비계구조', '지반조성')


def load_rows():
    rows = []
    for f in glob.glob(str(DATA / "scsbid" / "*.json")):
        for r in json.load(open(f, encoding="utf-8")).get("items", []):
            base, plan, amt, n = r.get("base"), r.get("plan"), r.get("amt"), r.get("cnt")
            if not (base and plan and amt and n and r.get("date")):
                continue
            S = plan / base * 100
            W = M.bid_to_sr(amt, base, r.get("a") or 0, r.get("floor") or 87.745)
            if W is None or not (0 <= W - S < 1):
                continue
            rows.append({"S": S, "W": W, "N": n, "rng": M.rng_key(r.get("rng")), "date": r["date"],
                         "org": r.get("org") or r.get("dmd") or "", "sido": r.get("sido") or "", "sgg": r.get("sgg") or "",
                         "ab": M.amt_bin(base), "fl": M.floor_key(r.get("floor")), "lic": "+".join(sorted(r.get("lic") or [])),
                         "sc": M.rgn_scope(r.get("rgn")), "id": r["id"], "base": base, "nm": r.get("nm", "")})
    rows.sort(key=lambda r: r["date"])
    return rows


def load_xls():
    files = [p for p in PRIV.glob("*.xls*") if p.name != "report.html"]   # 더비스 엑셀 (실제로는 HTML 표)
    if not files:
        return [], None
    path = max(files, key=lambda p: p.stat().st_mtime)
    t = path.read_text(encoding="utf-8", errors="ignore")
    out = []
    num = lambda s: (lambda v: float(v) if re.fullmatch(r"-?\d+(\.\d+)?", v) else None)(s.replace(",", "").strip())
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S)[1:]:
        c = [html.unescape(re.sub(r"<[^>]+>", "", x)).strip() for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        if len(c) < 13:
            continue
        rk, _, n = c[11].partition("/")
        out.append({"nm": c[1], "lic": c[3], "date": c[5][:10], "base": num(c[7]), "S": num(c[9]), "x": num(c[10]),
                    "rank": int(rk) if rk.lstrip("-").isdigit() else None, "N": int(n) if n.isdigit() else None,
                    "kind": "공사" if any(k in c[3] for k in CONSTR) and not c[3].startswith("[") else "물품"})
    return out, path.name


class Monthly:
    """시험 달 m 의 추천값: 그 달 이전 24개월만으로 학습 (model.py 와 같은 곡선)"""

    def __init__(self, rows):
        self.rows, self.cache = rows, {}

    def x(self, r):
        m = r["date"][:7]
        if m not in self.cache:
            start = dt.date.fromisoformat(m + "-01")
            ts = (start - dt.timedelta(days=int(30.44 * 24))).isoformat()
            train = [t for t in self.rows if ts <= t["date"] < m + "-01"]
            if len(train) < 300:
                self.cache[m] = None
            else:
                npm = M.fit_npred(train)
                M.with_lp(train, npm)
                self.cache[m] = (npm, M.Pools(train), {rk: float(np.mean([t["S"] for t in train if t["rng"] == rk] or [100])) for rk in M.RNGS})
        c = self.cache[m]
        if not c:
            return None, None
        npm, pools, means = c
        rk = r["rng"] if r["rng"] in M.RNGS else "-3,3"
        g = pools.get(rk, M.snap_key(M.predict_ln(npm, r)))
        return (None if g is None else float(M.G0 + int(np.argmax(g[0])) * M.GS)), means[rk]


def main():
    conf = json.loads((PRIV / "company.json").read_text(encoding="utf-8")) if (PRIV / "company.json").exists() else {}
    sido, lics = conf.get("sido", "강원"), set(conf.get("lics", []))
    rows = load_rows()
    mon = Monthly(rows)
    xls, xls_name = load_xls()
    their_x = np.array([t["x"] for t in xls if t["kind"] == "공사" and t["x"] and 95 < t["x"] < 105])
    months = [m for m, n in collections.Counter(r["date"][:7] for r in rows if r["rng"] in M.RNGS).items() if n >= 1000 and m >= "2025-07"]

    # 1) 우리 지역 × 우리 면허 전부
    sel = [r for r in rows if r["date"][:7] in months and r["sido"] == sido and r["rng"] in M.RNGS
           and (not lics or lics & set(r["lic"].split("+")))]
    res = []
    for r in sel:
        x, mean = mon.x(r)
        if x is not None:
            res.append((r, x, mean))
    fair = sum(1 / (r["N"] + 1) for r, _, _ in res)
    w_ours = sum(1 for r, x, _ in res if r["S"] <= x < r["W"])
    w_mean = sum(1 for r, _, m in res if r["S"] <= m < r["W"])
    w_their = float(np.mean([sum(1 for r, _, _ in res if r["S"] <= xx < r["W"]) for xx in their_x])) if len(their_x) else None
    seg = []
    for lo, hi in [(0, 50), (50, 150), (150, 400), (400, 10 ** 9)]:
        s = [(r, x) for r, x, _ in res if lo <= r["N"] < hi]
        if s:
            seg.append((f"{lo}~{hi if hi < 10 ** 9 else ''}", len(s), sum(1 for r, x in s if r["S"] <= x < r["W"]), sum(1 / (r["N"] + 1) for r, _ in s)))

    # 2) 회사 실제 공사 투찰 이력
    idx = collections.defaultdict(list)
    for r in rows:
        idx[(r["date"], int(r["base"]))].append(r)
    mine = []
    for t in (t for t in xls if t["kind"] == "공사" and t["base"]):
        cand = [c for dd in (0, -1, 1) for c in idx.get(((dt.date.fromisoformat(t["date"]) + dt.timedelta(days=dd)).isoformat(), int(t["base"])), [])]
        if cand:
            r = cand[0]
            x, _ = mon.x(r)
            if x is not None:
                mine.append((t, r, x))
    gong = [t for t in xls if t["kind"] == "공사"]
    m_fair = sum(1 / (r["N"] + 1) for _, r, _ in mine)
    m_ours = sum(1 for _, r, x in mine if r["S"] <= x < r["W"])
    m_real = sum(1 for t in xls if t["rank"] == 1)
    all_fair = sum(1 / (t["N"] + 1) for t in xls if t["N"])

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    ratio = lambda w, f: f"×{w / f:.2f}" if f else "-"
    rows_html = "".join(f"<tr><td>{a}</td><td class=n>{b}</td><td class=n>{c}</td><td class=n>{d:.1f}</td><td class=n>{c / b * 100:.1f}%</td></tr>" for a, b, c, d in seg)
    wins_html = "".join(f"<li>{t['date']} {html.escape(t['nm'][:40])} — 참가 {r['N']}곳, 우리 추천 {x:.3f}% (실제 {t['x']}%, 순위 {t['rank']})</li>"
                        for t, r, x in mine if r["S"] <= x < r["W"])
    body = f"""<!doctype html><html lang=ko><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>우리 업체 역검증</title><style>body{{font-family:'Noto Sans KR',system-ui,sans-serif;max-width:860px;margin:24px auto;padding:0 16px;color:#1B2430}}
h1{{font-size:22px}}h2{{font-size:17px;margin-top:28px}}table{{border-collapse:collapse;width:100%;margin:8px 0}}td,th{{border-bottom:1px solid #E4E8F0;padding:6px 8px;text-align:left}}
.n{{text-align:right;font-variant-numeric:tabular-nums}}.big{{font-size:20px;font-weight:700;color:#2F6FED}}.sub{{color:#6B7684;font-size:13px}}</style>
<h1>우리 업체 역검증 보고서</h1><p class=sub>{now} 자동 생성 · 데이터 {html.escape(json.load(open(DATA / 'meta.json', encoding='utf-8')).get('updated_at', '')[:16])} · 참고용이며 낙찰을 보장하지 않습니다</p>
<h2>1. {html.escape(sido)} × 우리 면허 공사 — 추천값으로 넣었다면</h2>
<p class=sub>매달 그 이전 24개월 자료만으로 정한 추천값(실전과 같은 조건). 시험 공고 {len(res):,}건, 참가 중앙값 {int(np.median([r['N'] for r, _, _ in res])) if res else '-'}곳</p>
<table><tr><th>방식</th><th class=n>낙찰</th><th class=n>적중률</th><th class=n>공정 기대 대비</th></tr>
<tr><td><b>우리 추천</b></td><td class=n><b>{w_ours}</b></td><td class=n>{w_ours / max(len(res), 1) * 100:.2f}%</td><td class=n><b>{ratio(w_ours, fair)}</b></td></tr>
<tr><td>평균 사정율 (사람들이 흔히 쏘는 값)</td><td class=n>{w_mean}</td><td class=n>{w_mean / max(len(res), 1) * 100:.2f}%</td><td class=n>{ratio(w_mean, fair)}</td></tr>
{f'<tr><td>더비스식 (회사 실제 투찰률 분포)</td><td class=n>{w_their:.1f}</td><td class=n>{w_their / max(len(res), 1) * 100:.2f}%</td><td class=n>{ratio(w_their, fair)}</td></tr>' if w_their is not None else ''}
<tr><td>공정 기대 Σ1/(참가+1)</td><td class=n>{fair:.1f}</td><td></td><td></td></tr></table>
<table><tr><th>참가 수</th><th class=n>공고</th><th class=n>우리 낙찰</th><th class=n>공정 기대</th><th class=n>우리 적중률</th></tr>{rows_html}</table>
<h2>2. 회사 실제 투찰 이력 ({html.escape(xls_name or '파일 없음')})</h2>
<p>전체 {len(xls):,}건 (공사 {len(gong)}, 물품 {len(xls) - len(gong)}) · 실제 1순위 <b>{m_real}</b>건 · 공정 기대 {all_fair:.2f}건</p>
<p>공사 {len(gong)}건 중 수집 낙찰과 맞춘 {len(mine)}건: 우리 추천이었다면 <span class=big>{m_ours}건</span> 낙찰 (공정 기대 {m_fair:.2f}건)</p>
<ul>{wins_html or '<li>없음</li>'}</ul>
<p class=sub>못 맞춘 공사는 수집이 아직 안 된 달(빈 달 다시 받기 진행 중)이거나 나라장터 밖 공고. 물품은 물품 모델 검증 뒤 추가.</p></html>"""
    PRIV.mkdir(exist_ok=True)
    (PRIV / "report.html").write_text(body, encoding="utf-8")
    print(f"report: {sido}×면허 {len(res)}건 우리 {w_ours} / 평균 {w_mean} / 더비스식 {w_their} / 기대 {fair:.1f} · 이력 맞춤 {len(mine)}건 우리 {m_ours}")


if __name__ == "__main__":
    main()

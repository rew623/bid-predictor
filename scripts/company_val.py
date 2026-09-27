#!/usr/bin/env python3
"""우리 지역 × 우리 면허 공사 역검증 → data/company_val.json (앱 내 투찰 '🩺 진단'에 표시, 수집 워크플로에서 하루 한 번)

company_report.py 의 1) 부분을 공개 데이터만으로 GitHub Actions 에서 돌린다(2026-09-27 — PC 작업 스케줄러 대신).
매달 그 이전 24개월 자료만으로 정한 추천값(model.py 와 같은 곡선)으로 넣었다면 몇 건 낙찰됐나 vs 평균 사정율 vs 공정 기대 Σ1/(참가+1).
회사 실제 투찰 이력(더비스 엑셀) 비교는 저장소에 올리지 않고 앱의 🩺 진단(엑셀 가져오기)으로 본다.

설정: scripts/company.json {"sido": "강원", "lics": [...]} (공개해도 되는 값만 — 사업자번호 넣지 말 것)
"""
import collections
import datetime as dt
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import company_report as C  # noqa: E402
import model as M  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONF, OUT = ROOT / "scripts" / "company.json", ROOT / "data" / "company_val.json"


def pois_ci(k):
    """포아송 95% 구간 (Wilson–Hilferty 근사, 앱 poisCI 와 같음)"""
    z = 1.96
    lo = 0.0 if k == 0 else k * (1 - 1 / (9 * k) - z / (3 * math.sqrt(k))) ** 3
    k1 = k + 1
    return lo, k1 * (1 - 1 / (9 * k1) + z / (3 * math.sqrt(k1))) ** 3


def main():
    conf = json.loads(CONF.read_text(encoding="utf-8"))
    sido, lics = conf["sido"], set(conf.get("lics", []))
    rows = C.load_rows()
    mon = C.Monthly(rows)
    since = (dt.date.today().replace(day=1) - dt.timedelta(days=int(30.44 * conf.get("months", 15)))).isoformat()[:7]
    months = sorted(m for m, n in collections.Counter(r["date"][:7] for r in rows if r["rng"] in M.RNGS).items() if n >= 1000 and m >= since)
    sel = [r for r in rows if r["date"][:7] in months and r["sido"] == sido and r["rng"] in M.RNGS and (not lics or lics & set(r["lic"].split("+")))]
    res = []
    for r in sel:
        x, mean = mon.x(r)
        if x is not None:
            res.append((r, x, mean))
    win = lambda r, x: r["S"] <= x < r["W"]
    fair = sum(1 / (r["N"] + 1) for r, _, _ in res)
    ours = sum(win(r, x) for r, x, _ in res)
    mean = sum(win(r, m) for r, _, m in res)
    lo, hi = pois_ci(ours)
    seg = []
    for a, b in [(0, 50), (50, 150), (150, 400), (400, 10 ** 9)]:
        s = [(r, x) for r, x, _ in res if a <= r["N"] < b]
        if s:
            seg.append({"k": f"{a}~{b - 1 if b < 10 ** 9 else ''}", "n": len(s), "ours": sum(win(r, x) for r, x in s), "fair": round(sum(1 / (r["N"] + 1) for r, _ in s), 2)})
    by_m = collections.defaultdict(lambda: [0, 0, 0, 0.0])
    for r, x, m in res:
        t = by_m[r["date"][:7]]
        t[0] += 1; t[1] += win(r, x); t[2] += win(r, m); t[3] += 1 / (r["N"] + 1)
    out = {"v": 1, "updated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="seconds"),
           "sido": sido, "lics": sorted(lics), "from": months[0] if months else None, "to": months[-1] if months else None,
           "n": len(res), "median_N": int(sorted(r["N"] for r, _, _ in res)[len(res) // 2]) if res else None,
           "ours": ours, "mean": mean, "fair": round(fair, 2), "ci": [round(lo, 2), round(hi, 2)], "seg": seg,
           "months": [{"m": m, "n": t[0], "ours": t[1], "mean": t[2], "fair": round(t[3], 2)} for m, t in sorted(by_m.items())],
           "rule": "매달 그 이전 24개월 자료만으로 정한 추천값(표본외). 낙찰 = S ≤ 추천 투찰 사정률 < 실제 1위 투찰 사정률"}
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"company_val: {sido}×면허 {len(res)}건 · 우리 {ours} / 평균 사정율 {mean} / 공정 기대 {fair:.1f} (95% {lo:.1f}~{hi:.1f})")


if __name__ == "__main__":
    main()

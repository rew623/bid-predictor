"""물품 추천 투찰 사정률 → data/thng_rec.json (2026-09-29, 사용자 승인 'A로 넣기')

규칙(국방에서 미리 정하고 나라장터로 다시 확인한 것 그대로 — 설정을 바꾸지 말 것):
  이전 달들의 '추첨 평균 1순위 확률 곡선'(복수예가 1,365가지 조합 평균, thng_curve.lottery_curve)을 전부 더해
  ±0.1%p 이동평균의 최대점 = 추천 x. 참가 규모는 나누지 않는다(나눠도 위치가 거의 같음 — 나라장터 99.12~99.14, 국방 101.1~101.4).
  - 나라장터: data/thng_curve.json (참가 50곳↑ 공고만 모음). 참가 50곳 미만 공고는 강원 물품 역검증(고정 101.0 ×1.66)대로 101.0.
  - 국방: data/d2b/{연도}/{월}.json 물품 중 전원 투찰률(c·x)·복수예가 15개가 있는 공고.
역검증: 달마다 그 이전 달로만 x 를 정해 그 달에 적용(표본외) → 추첨 평균 기대 1순위 수 ÷ 공정 기대 Σ1/(참가+1).
{"v":1, "updated_at", "g2b":{"x", "small_x", "val":{"n","fair","exp","real","lo","hi","months":[[월, ×]]}}, "d2b":{"x", "val":{…}}}
"""
import datetime as dt
import glob
import itertools
import json
from pathlib import Path

import numpy as np

from thng_curve import X0, STEP, NG

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
GRID = X0 + STEP * np.arange(NG)
W = 10   # ±0.1%p
SMALL_X = 101.0
COMB = np.array(list(itertools.combinations(range(15), 4)))


def smooth(v):
    return np.convolve(v, np.ones(2 * W + 1) / (2 * W + 1), mode="same")


def poisson_ci(k):
    q = lambda kk, z: kk * (1 - 1 / (9 * kk) + z / (3 * kk ** 0.5)) ** 3 if kk > 0 else 0.0
    return q(k, -1.96), q(k + 1, 1.96)


def rolling(month_curves):
    """month_curves: {월: (곡선 합, 실제 적중 곡선 합 | None, 공고 수, 공정 기대 합)} → (최신 x, 검증)"""
    months = sorted(month_curves)
    exp = fair = real = n = 0.0
    per = []
    for i, m in enumerate(months[1:], 1):
        tr = sum(month_curves[p][0] for p in months[:i])
        x = int(np.argmax(smooth(tr)))
        c, rc, nn, f = month_curves[m]
        exp += c[x]; fair += f; n += nn
        if rc is not None:
            real += rc[x]
        per.append([m, round(float(c[x] / f), 2) if f else None])
    allc = sum(v[0] for v in month_curves.values())
    x = round(float(GRID[int(np.argmax(smooth(allc)))]), 2)
    lo, hi = poisson_ci(real)
    val = {"n": int(n), "fair": round(fair, 1), "exp": round(float(exp), 1), "real": int(real),
           "lo": round(lo / fair, 2) if fair else None, "hi": round(hi / fair, 2) if fair else None, "months": per}
    return x, val


def g2b():
    t = json.load(open(DATA / "thng_curve.json", encoding="utf-8"))
    mc = {}
    for k, a in t["agg"].items():
        m = k.split("|")[0]
        c, rc, n, f = mc.get(m, (np.zeros(NG), np.zeros(NG), 0, 0.0))
        mc[m] = (c + np.asarray(a["c"]), rc + np.asarray(a["rc"]), n + a["n"], f + a["fair"])
    x, val = rolling(mc)
    return {"x": x, "small_x": SMALL_X, "val": val}


def d2b():
    mc = {}
    for p in sorted(glob.glob(str(DATA / "d2b" / "20*" / "*.json"))):
        for b in json.load(open(p, encoding="utf-8")).get("items") or []:
            if b.get("kind") != "물품" or not b.get("c") or not b.get("x") or len(b.get("p") or []) != 15 or not (b.get("base") and b.get("floor")):
                continue
            base, fl = b["base"], b["floor"]
            P = np.array([q[1] for q in b["p"]], float)
            plan_c = P[COMB].mean(1)
            plan = b.get("plan") or plan_c.mean()
            rates = np.cumsum(b["x"]) / 1000.0
            k = b.get("k") or 0
            keep = np.array([i < k or r < fl for i, r in enumerate(rates)])   # 순위 있는 업체 + 하한 미달(추첨에 따라 유효해질 수 있음)
            A = np.sort((rates / 100 * plan)[keep])
            if not len(A):
                continue
            F = plan_c * fl / 100
            i = np.searchsorted(A, F)
            Wc = np.where(i < len(A), A[np.minimum(i, len(A) - 1)], np.inf)
            unit = base * fl / 100 / 100
            d = np.zeros(NG + 1)
            np.add.at(d, np.clip(np.searchsorted(GRID, F / unit - 1e-9), 0, NG), 1)
            np.add.at(d, np.clip(np.searchsorted(GRID, Wc / unit - 1e-9), 0, NG), -1)
            curve = np.cumsum(d)[:-1] / len(COMB)
            n = b.get("cnt") or len(b["c"])
            m = b["date"][:7]
            c, _, nn, f = mc.get(m, (np.zeros(NG), None, 0, 0.0))
            mc[m] = (c + curve, None, nn + 1, f + 1 / (n + 1))
    if len(mc) < 2:
        return None
    x, val = rolling(mc)
    val.pop("real"); val.pop("lo"); val.pop("hi")   # 국방은 실제 추첨 결과 대조를 여기서 안 함(2026-09-28 따로 확인: 대체로 맞음)
    return {"x": x, "val": val}


def main():
    out = {"v": 1, "updated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="seconds")}
    try:
        out["g2b"] = g2b()
    except Exception as e:  # noqa: BLE001
        print("나라장터 물품 추천 실패", e)
    try:
        d = d2b()
        if d:
            out["d2b"] = d
    except Exception as e:  # noqa: BLE001
        print("국방 물품 추천 실패", e)
    p = DATA / "thng_rec.json"
    old = json.load(open(p, encoding="utf-8")) if p.exists() else {}
    if {k: v for k, v in old.items() if k != "updated_at"} == {k: v for k, v in out.items() if k != "updated_at"}:
        print("[물품 추천] 바뀐 것 없음")
        return
    p.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print("[물품 추천]", {k: (v.get("x"), v.get("val", {}).get("exp"), v.get("val", {}).get("fair")) for k, v in out.items() if isinstance(v, dict)})


if __name__ == "__main__":
    main()

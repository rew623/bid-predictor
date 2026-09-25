#!/usr/bin/env python3
"""수집 뒤 실행: 전국 과거 낙찰로 추천 모델을 만들고 표본외 역검증까지 해서 data/model.json 에 쓴다.

1) 참가업체 수 예측표  — 금액대×예가범위 → 시도×금액대 → 시도×금액대×하한율 → 면허 → 시도×면허 → 기관 순으로 로그 평균을 수축(shrink)해 섞는다.
2) 낙찰확률 곡선        — 예가범위 × 예상 참가수(로그 0.2 간격)마다, 참가수가 비슷한(×1/4~×4) 최근 24개월 전국 공고의
                         승리 구간 [S, W) 를 쌓아 97~103% 0.001 간격으로 계산, ±0.01%p 이동평균. 최대점 = 추천 투찰 사정률.
3) 역검증              — 최근 12개월 각 달을 그 이전 데이터만으로 추천해, 실제로 1순위였는지 센다.
                         비교: 평균 사정율 방식, 무작위(평균 업체 = 1/참가수).
근거: 2026-07~09 실데이터 분석에서 '비슷한 경쟁 규모로 학습'이 평균 사정율 방식보다 새 달에서 더 자주 이겼고(9월 138 vs 124건),
     예상 참가수 하위 20% 공고만 넣으면 낙찰률이 약 2배였다. 설계 근거·한계는 CLAUDE.md.
"""
import collections
import datetime as dt
import glob
import json
import math
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
G0, GS, GN = 97.0, 0.001, 6000
SMOOTH_K = 10            # ±0.01%p
BAND = 4.0               # 참가수 ×1/4 ~ ×4
MIN_POOL = 200
KEY_STEP = 2             # 곡선 키 = round(10·ln 참가수), 2 간격(로그 0.2)
KEYS = list(range(10, 81, KEY_STEP))   # 참가수 약 3 ~ 3000
AMT_EDGES = [7.7, 8.0, 8.3, 8.6, 9.0, 9.5]
SHRINK = 5
TRAIN_MONTHS = 24
VALID_MONTHS = 12
RNGS = ["-2,2", "-3,3"]


def bid_to_sr(amt, base, a, floor):
    x = ((amt - a) / (floor / 100) + a) / base * 100
    return x if 90 < x < 110 else None


def rng_key(r):
    return ",".join("%g" % v for v in (r or []))


def amt_bin(base):
    lg = math.log10(base)
    return sum(1 for e in AMT_EDGES if lg >= e)


def floor_key(f):
    return "%g" % f if f else ""


def load_rows():
    rows = []
    for f in glob.glob(str(DATA / "scsbid" / "*.json")):
        for r in json.load(open(f, encoding="utf-8")).get("items", []):
            base, plan, amt, n = r.get("base"), r.get("plan"), r.get("amt"), r.get("cnt")
            if not (base and plan and amt and n and r.get("date")):
                continue
            S = plan / base * 100
            W = bid_to_sr(amt, base, r.get("a") or 0, r.get("floor") or 87.745)
            if W is None or not (0 <= W - S < 1):
                continue
            rows.append({"S": S, "W": W, "N": n, "rng": rng_key(r.get("rng")), "date": r["date"],
                         "org": r.get("org") or r.get("dmd") or "", "sido": r.get("sido") or "",
                         "ab": amt_bin(base), "fl": floor_key(r.get("floor")), "lic": "+".join(sorted(r.get("lic") or []))})
    rows.sort(key=lambda r: r["date"])
    return rows


# ---------------------------------------------------------------- 참가수 예측
# 넓은 → 좁은 순. 면허를 넣으면 참가수 예측 상관이 0.62 → 0.67 (2026-09 표본외)
PRED_KEYS = [
    ("a", lambda r: f'{r["ab"]}|{r["rng"]}'),
    ("sa", lambda r: f'{r["sido"]}|{r["ab"]}'),
    ("saf", lambda r: f'{r["sido"]}|{r["ab"]}|{r["fl"]}'),
    ("l", lambda r: r["lic"]),
    ("sl", lambda r: f'{r["sido"]}|{r["lic"]}' if r["lic"] else ""),
    ("o", lambda r: r["org"]),
]


def fit_npred(rows):
    ln = [math.log(r["N"]) for r in rows]
    g = sum(ln) / len(ln)
    tables = {}
    for name, kf in PRED_KEYS:
        acc = collections.defaultdict(lambda: [0.0, 0])
        for r, v in zip(rows, ln):
            k = kf(r)
            if k and not k.startswith("|"):
                acc[k][0] += v
                acc[k][1] += 1
        tables[name] = {k: [round(s / n, 4), n] for k, (s, n) in acc.items()}
    model = {"g": round(g, 4), "shrink": SHRINK, "t": tables}
    res = [v - predict_ln(model, r) for r, v in zip(rows, ln)]
    model["sd"] = round(float(np.std(res)), 4)
    return model


def predict_ln(model, r):
    est = model["g"]
    for name, kf in PRED_KEYS:
        m, n = model["t"][name].get(kf(r), (est, 0))
        est = (n * m + model["shrink"] * est) / (n + model["shrink"])
    return est


# ---------------------------------------------------------------- 곡선
def curve(S, W):
    diff = np.zeros(GN + 2)
    i0 = np.clip(np.ceil((S - G0) / GS - 1e-9).astype(int), 0, GN + 1)
    i1 = np.clip(np.ceil((W - G0) / GS - 1e-9).astype(int), 0, GN + 1)
    np.add.at(diff, i0, 1.0)
    np.add.at(diff, i1, -1.0)
    raw = np.cumsum(diff)[:GN + 1] / len(S)
    cs = np.concatenate([[0], np.cumsum(raw)])
    idx = np.arange(GN + 1)
    lo, hi = np.clip(idx - SMOOTH_K, 0, GN), np.clip(idx + SMOOTH_K, 0, GN)
    return (cs[hi + 1] - cs[lo]) / (hi - lo + 1)


class Pools:
    """학습 행들로 (예가범위, 곡선키) 별 곡선을 만들고 재사용"""

    def __init__(self, rows):
        self.by_rng = {}
        for rk in RNGS:
            sel = [r for r in rows if r["rng"] == rk]
            self.by_rng[rk] = (np.array([r["S"] for r in sel]), np.array([r["W"] for r in sel]), np.array([r["N"] for r in sel], float))
        self.cache = {}

    def get(self, rk, key):
        if (rk, key) not in self.cache:
            S, W, N = self.by_rng[rk]
            if len(S) < 30:
                self.cache[(rk, key)] = None
            else:
                c = math.exp(key / 10)
                m = (N >= c / BAND) & (N <= c * BAND)
                if m.sum() < MIN_POOL:
                    m = np.ones(len(S), bool)
                sm = curve(S[m], W[m])
                self.cache[(rk, key)] = (sm, S[m], N[m])
        return self.cache[(rk, key)]


def snap_key(ln_n):
    k = round(ln_n * 10 / KEY_STEP) * KEY_STEP
    return min(max(k, KEYS[0]), KEYS[-1])


def summarize(sm, S, N):
    bi = int(np.argmax(sm))
    lo = hi = bi
    while lo > 0 and sm[lo - 1] >= sm[bi] * 0.9:
        lo -= 1
    while hi < GN and sm[hi + 1] >= sm[bi] * 0.9:
        hi += 1
    peaks = []
    for i in np.argsort(-sm):
        if sm[i] <= 0 or len(peaks) >= 3:
            break
        if all(abs(i - p) > 50 for p in peaks):
            peaks.append(int(i))
    x = lambda i: round(G0 + i * GS, 3)
    qs = np.quantile(S, [0.01, 0.99])
    return {"x": x(bi), "p": round(float(sm[bi]), 5), "lo": x(lo), "hi": x(hi), "n": int(len(S)),
            "r": round(float(np.mean(1 / N)), 5), "cnt": round(float(np.median(N)), 1),
            "pk": [[x(i), round(float(sm[i]), 5)] for i in peaks],
            "v": [round(max(G0, min(float(qs[0]), x(bi) - 0.3)), 1), round(min(G0 + GN * GS, max(float(qs[1]), x(bi) + 0.3)), 1)],
            "c": [int(round(v * 1e5)) for v in sm[::10]]}   # 0.01 간격 601칸, ×1e5 정수


# ---------------------------------------------------------------- 역검증
def wilson(k, n, z=1.96):
    if not n:
        return [0, 0]
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(c - h, 5), round(c + h, 5)]


def validate(rows):
    months = sorted({r["date"][:7] for r in rows})
    tests = [m for m in months[1:]][-VALID_MONTHS:]
    segs = [0, 20, 40, 80, 150, 10 ** 9]
    out_m, seg = [], [{"k": [segs[i], segs[i + 1] if segs[i + 1] < 10 ** 9 else None], "n": 0, "near": 0, "mean": 0, "rand": 0.0} for i in range(len(segs) - 1)]
    for m in tests:
        start = dt.date.fromisoformat(m + "-01")
        tstart = (start.replace(day=1) - dt.timedelta(days=int(30.44 * TRAIN_MONTHS))).isoformat()
        train = [r for r in rows if tstart <= r["date"] < m + "-01"]
        test = [r for r in rows if r["date"][:7] == m and r["rng"] in RNGS]
        if len(train) < 300 or not test:
            continue
        npm = fit_npred(train)
        pools = Pools(train)
        means = {rk: float(np.mean(pools.by_rng[rk][0])) if len(pools.by_rng[rk][0]) else 100.0 for rk in RNGS}
        row = {"m": m, "n": 0, "near": 0, "mean": 0, "rand": 0.0, "below_near": 0, "below_mean": 0}
        for r in test:
            ln_p = predict_ln(npm, r)
            got = pools.get(r["rng"], snap_key(ln_p))
            if got is None:
                continue
            xn = G0 + int(np.argmax(got[0])) * GS
            xm = means[r["rng"]]
            hn, hm = r["S"] <= xn < r["W"], r["S"] <= xm < r["W"]
            row["n"] += 1
            row["near"] += hn
            row["mean"] += hm
            row["rand"] += 1 / r["N"]
            row["below_near"] += xn < r["S"]
            row["below_mean"] += xm < r["S"]
            pn = math.exp(ln_p)
            s = seg[next(i for i in range(len(segs) - 1) if segs[i] <= pn < segs[i + 1])]
            s["n"] += 1
            s["near"] += hn
            s["mean"] += hm
            s["rand"] += 1 / r["N"]
        row["rand"] = round(row["rand"], 2)
        out_m.append(row)
    for s in seg:
        s["rand"] = round(s["rand"], 2)
    n = sum(r["n"] for r in out_m)
    tot = {"n": n, "near": sum(r["near"] for r in out_m), "mean": sum(r["mean"] for r in out_m),
           "rand": round(sum(r["rand"] for r in out_m), 2)}
    if n:
        tot["near_ci"] = wilson(tot["near"], n)
        tot["mean_ci"] = wilson(tot["mean"], n)
        tot["below_near"] = round(sum(r["below_near"] for r in out_m) / n, 4)
        tot["below_mean"] = round(sum(r["below_mean"] for r in out_m) / n, 4)
    return {"months": out_m, "segments": [s for s in seg if s["n"]], "total": tot,
            "rule": f"참가수 ×1/{BAND:g}~×{BAND:g} 비슷한 공고, 곡선 ±{SMOOTH_K * GS:g}%p, 학습 = 시험 달 이전 {TRAIN_MONTHS}개월"}


def main():
    rows = load_rows()
    if len(rows) < 300:
        print("model: 표본 부족, 건너뜀", len(rows))
        return
    last = rows[-1]["date"]
    cut = (dt.date.fromisoformat(last) - dt.timedelta(days=int(30.44 * TRAIN_MONTHS))).isoformat()
    recent = [r for r in rows if r["date"] >= cut]
    pools = Pools(recent)
    curves = {}
    for rk in RNGS:
        lst = []
        for k in KEYS:
            got = pools.get(rk, k)
            if got is not None:
                lst.append({"k": k, **summarize(*got)})
        curves[rk] = lst
    model = {
        "v": 1,
        "updated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="seconds"),
        "data": {"from": recent[0]["date"], "to": last, "rows": len(recent)},
        "grid": {"x0": G0, "step": 0.01, "n": 601, "scale": 1e5},
        "band": BAND, "amt_edges": AMT_EDGES,
        "meanS": {rk: round(float(np.mean(pools.by_rng[rk][0])), 4) for rk in RNGS if len(pools.by_rng[rk][0])},
        "npred": fit_npred(recent),
        "curves": curves,
        "validation": validate(rows),
    }
    text = json.dumps(model, ensure_ascii=False, separators=(",", ":"))
    path = DATA / "model.json"
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
        old["updated_at"] = model["updated_at"]
        if json.dumps(old, ensure_ascii=False, separators=(",", ":")) == text:
            print("model: 변경 없음")
            return
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    path.write_text(text, encoding="utf-8")
    # 앱 캐시가 새 모델을 받도록 meta 의 갱신 시각도 올린다
    mpath = DATA / "meta.json"
    try:
        meta = json.loads(mpath.read_text(encoding="utf-8"))
        meta["updated_at"] = model["updated_at"]
        meta["model"] = {"updated_at": model["updated_at"], "rows": len(recent)}
        mpath.write_text(json.dumps(meta, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    v = model["validation"]["total"]
    print(f'model: {len(recent)}건, {len(text) / 1024:.0f}KB, 역검증 {v.get("n")}건 비슷한경쟁 {v.get("near")} / 평균 {v.get("mean")} / 무작위기대 {v.get("rand")}')


if __name__ == "__main__":
    main()

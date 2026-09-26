#!/usr/bin/env python3
"""수집 뒤 실행: 전국 과거 낙찰로 추천 모델을 만들고 표본외 역검증까지 해서 data/model.json 에 쓴다.

1) 참가업체 수 예측표  — 금액대×예가범위 → 시도×금액대 → 시도×금액대×하한율 → 면허 → 시도×면허 → 기관 순으로 로그 평균을 수축(shrink)해 섞는다.
2) 낙찰확률 곡선        — 예가범위 × 예상 참가수(로그 0.2 간격)마다, *예상* 참가수가 비슷한(로그 ±0.4) 최근 24개월 전국 공고의
                         승리 구간 [S, W) 를 쌓아 97~103% 0.001 간격으로 계산, ±0.2%p 이동평균. 최대점 = 추천 투찰 사정률.
3) 역검증              — 최근 12개월 각 달을 그 이전 데이터만으로 추천해, 실제로 1순위였는지 센다.
                         비교: 평균 사정율 방식, 무작위(평균 업체 = 1/참가수).
근거(2026-09 재검토, 11개월 60,534건 표본외): 실제 참가수로 곡선을 고르던 방식은 쓸 때 예측 참가수로 고르므로 어긋나
     평균 업체 대비 ×0.90 이었다. 예측 참가수로 학습 + ±0.2%p 로 바꾸면 ×1.00 (11개월 중 10개월 개선), 예상 150곳↑ 는 ×1.3 안팎.
     참가수를 정확히 알아도 상한은 ×1.12 — 위치 선택만으로 큰 우위는 없다. 설계 근거·한계는 CLAUDE.md.
"""
import collections
import datetime as dt
import glob
import json
import math
import re
from pathlib import Path

import numpy as np

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from korea import parse_region  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
G0, GS, GN = 97.0, 0.001, 6000
SMOOTH_K = 200           # ±0.2%p (±0.01 은 봉우리가 우연이라 새 달에서 못 이김)
LN_BW = 0.4              # 예측 참가수 로그 ±0.4 (×0.67~×1.49). 0.6·0.8 은 시험 기간에 따라 ±2% 엇갈려 잡음 수준(2026-09)
MIN_POOL = 300
KEY_STEP = 2             # 곡선 키 = round(10·ln 참가수), 2 간격(로그 0.2)
KEYS = list(range(10, 81, KEY_STEP))   # 참가수 약 3 ~ 3000
AMT_EDGES = [7.7, 8.0, 8.3, 8.6, 9.0, 9.5]
SHRINK = 5
ADJ_K = 10               # 보정층 수축 (n / (n + 10))
ADJ_MIN = 3              # 보정층 표에 남길 최소 건수 (용량)
TRAIN_MONTHS = 24
VALID_MONTHS = 12
RNGS = ["-2,2", "-3,3"]


def bid_to_sr(amt, base, a, floor):
    x = ((amt - a) / (floor / 100) + a) / base * 100
    return x if 90 < x < 110 else None


def rng_key(r):
    return ",".join("%g" % v for v in (r or []))


def rgn_scope(rgn):
    """참가가능지역 → 범위 코드. n 모름·제한없음 / g 시·군 하나 / G 시·군 2~4 / s 시·도 하나 / S 시·도 여럿.
    시·군 제한이면 참가가 적고(로그 −0.4) 시·도 전체면 많다(+0.35). 앱 rgnScope 와 같은 규칙"""
    if not rgn:
        return "n"
    pairs, sidos, sido_level = set(), set(), False
    for t in rgn:
        sd = parse_region(t)[0]
        tok = t.split()
        head = tok[0] if tok else ""   # 시·도는 원문 첫 단어로, 시·군·구는 둘째 단어로 (앱 parseRegion 과 똑같이)
        sg = tok[1] if len(tok) > 1 and re.fullmatch(r"\S+[시군구]", tok[1]) else None
        if sd:
            sidos.add(head)
        if sg:
            pairs.add((head, sg))
        elif sd:
            sido_level = True
    if pairs and not sido_level and len(pairs) <= 4:
        return "g" if len(pairs) == 1 else "G"
    return "s" if len(sidos) <= 1 else "S"


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
                         "org": r.get("org") or r.get("dmd") or "", "sido": r.get("sido") or "", "sgg": r.get("sgg") or "",
                         "ab": amt_bin(base), "fl": floor_key(r.get("floor")), "lic": "+".join(sorted(r.get("lic") or [])),
                         "sc": rgn_scope(r.get("rgn"))})
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


# 보정층: 위 표로 예측한 뒤 남은 차이(잔차)를 순서대로 더한다.
# hl = 같은 시·군 × 같은 면허 공고의 과거 참가수 (표본외 참가수 예측 상관 0.66 → 0.74, 예측 하위 20% 공고의 평균 1/참가수 6.4% → 8.0%)
# r, rs = 참가가능지역 범위 (2026-09 교차검증 상관 +0.03~0.05). 수집기 '지역보강' 단계가 과거 레코드에 rgn 을 채우는 만큼 효과가 커진다
ADJ_KEYS = [
    ("hl", lambda r: f'{r["sido"]}|{r["sgg"]}|{r["lic"]}' if r["sgg"] else ""),
    ("r", lambda r: r["sc"]),
    ("rs", lambda r: f'{r["sc"]}|{r["sido"]}|{r["ab"]}'),
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
    est = [predict_ln(model, r) for r in rows]
    adj = {}
    for name, kf in ADJ_KEYS:
        acc = collections.defaultdict(lambda: [0.0, 0])
        for r, v, e in zip(rows, ln, est):
            k = kf(r)
            if k:
                acc[k][0] += v - e
                acc[k][1] += 1
        t = {k: [round(s / n, 3), n] for k, (s, n) in acc.items() if n >= ADJ_MIN}
        adj[name] = t
        est = [e + adj_add(t, kf(r)) for r, e in zip(rows, est)]
    model["adj"] = {"k": ADJ_K, "t": adj}
    res = [v - predict_ln(model, r) for r, v in zip(rows, ln)]
    model["sd"] = round(float(np.std(res)), 4)
    return model


def adj_add(t, k):
    m, n = t.get(k, (0, 0)) if k else (0, 0)
    return n * m / (n + ADJ_K)


def predict_ln(model, r):
    est = model["g"]
    for name, kf in PRED_KEYS:
        m, n = model["t"][name].get(kf(r), (est, 0))
        est = (n * m + model["shrink"] * est) / (n + model["shrink"])
    for name, kf in ADJ_KEYS if "adj" in model else []:
        est += adj_add(model["adj"]["t"][name], kf(r))
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
    """학습 행들로 (예가범위, 곡선키) 별 곡선을 만들고 재사용. 행은 예측 참가수(로그) "lp" 로 고른다 —
    쓸 때도 예측 참가수로 곡선을 고르므로 학습도 같은 기준이어야 한다 (실제 참가수로 고르면 표본외 ×0.90)"""

    def __init__(self, rows):
        self.by_rng = {}
        for rk in RNGS:
            sel = [r for r in rows if r["rng"] == rk]
            self.by_rng[rk] = (np.array([r["S"] for r in sel]), np.array([r["W"] for r in sel]),
                               np.array([r["N"] for r in sel], float), np.array([r["lp"] for r in sel]))
        self.cache = {}

    def get(self, rk, key):
        if (rk, key) not in self.cache:
            S, W, N, L = self.by_rng[rk]
            if len(S) < 30:
                self.cache[(rk, key)] = None
            else:
                d = np.abs(L - key / 10)
                m = d <= LN_BW
                if m.sum() < MIN_POOL:
                    m = np.argsort(d)[:MIN_POOL]
                sm = curve(S[m], W[m])
                self.cache[(rk, key)] = (sm, S[m], N[m])
        return self.cache[(rk, key)]


def with_lp(rows, npm):
    for r in rows:
        r["lp"] = predict_ln(npm, r)
    return rows


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
        if all(abs(i - p) > SMOOTH_K for p in peaks):   # 후보끼리 곡선 폭 이상 떨어지게
            peaks.append(int(i))
    x = lambda i: round(G0 + i * GS, 3)
    qs = np.quantile(S, [0.01, 0.99])
    return {"x": x(bi), "p": round(float(sm[bi]), 5), "lo": x(lo), "hi": x(hi), "n": int(len(S)),
            "r": round(float(np.mean(1 / (N + 1))), 5), "cnt": round(float(np.median(N)), 1),
            "sb": round(float(np.mean(S > G0 + bi * GS)), 3),   # 추천값이 하한 미달(사정율보다 낮음)이던 비율
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
        pools = Pools(with_lp(train, npm))
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
            row["rand"] += 1 / (r["N"] + 1)   # 공정 기대: 우리가 들어가면 참가자가 하나 늘어난다
            row["below_near"] += xn < r["S"]
            row["below_mean"] += xm < r["S"]
            pn = math.exp(ln_p)
            s = seg[next(i for i in range(len(segs) - 1) if segs[i] <= pn < segs[i + 1])]
            s["n"] += 1
            s["near"] += hn
            s["mean"] += hm
            s["rand"] += 1 / (r["N"] + 1)
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
            "rule": f"예상 참가수 ×{math.exp(-LN_BW):.2f}~×{math.exp(LN_BW):.2f} 비슷한 공고, 곡선 ±{SMOOTH_K * GS:g}%p, 학습 = 시험 달 이전 {TRAIN_MONTHS}개월"}


# ---------------------------------------------------------------- 지역 전용 추천 (scripts/local_models.json)
# 지역(예: 강원 춘천시) 공고에는 전국 곡선 대신 그 지역 공고로 만든 곡선이 더 맞을 수 있다(늘 오는 지역 업체들이 몰리는 자리).
# 후보: 시·도(sido)·시·군(sgg) 공고를 같은 규모(예상 참가 small 미만/이상)끼리. 매일 표본외로 전국 모델과 비교해
# 확실히 더 이긴 후보만 쓴다(낙찰 +10%↑ 그리고 +2건↑). 자료가 쌓이면 판정이 저절로 바뀐다.
LOCAL_CONF = ROOT / "scripts" / "local_models.json"
LOCAL_MIN = 30          # 곡선 하나에 필요한 최소 공고 수
LOCAL_LEVELS = ("sido", "sgg")


LOCAL_ALPHA = 0.05 / 4   # 후보 4개(시도·시군 × small·big)를 한꺼번에 보므로 본페로니


def _sign_p(b, c):
    """한쪽 부호검정: 엇갈린 b+c 건 중 지역만 낙찰이 b 건 이상일 확률(차이 없다는 가정에서)"""
    n = b + c
    if n == 0:
        return 1.0
    return sum(math.comb(n, k) for k in range(b, n + 1)) / 2 ** n


def _local_pool(rows, area, level, rk, seg, small):
    return [r for r in rows if r["rng"] == rk and r["sido"] == area["sido"] and (level == "sido" or r["sgg"] == area["sgg"])
            and ("small" if math.exp(r["lp"]) < small else "big") == seg]


def _local_x(pool):
    if len(pool) < LOCAL_MIN:
        return None
    return G0 + int(np.argmax(curve(np.array([r["S"] for r in pool]), np.array([r["W"] for r in pool])))) * GS


def local_models(rows, recent, npred):
    try:
        conf = json.loads(LOCAL_CONF.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    small = conf.get("small", 50)
    months = sorted({r["date"][:7] for r in rows})
    tests = [m for m in months[1:]][-VALID_MONTHS:]
    out = {}
    for area in conf.get("areas", []):
        tal = {seg: {k: 0.0 for k in ("n", "fair", "national", *LOCAL_LEVELS, *[f"{lv}_{s}" for lv in LOCAL_LEVELS for s in "bc"])} for seg in ("small", "big")}
        for m in tests:
            start = dt.date.fromisoformat(m + "-01")
            tstart = (start - dt.timedelta(days=int(30.44 * TRAIN_MONTHS))).isoformat()
            test = [r for r in rows if r["date"][:7] == m and r["rng"] in RNGS and r["sido"] == area["sido"] and r["sgg"] == area["sgg"]]
            if not test:
                continue
            train = [r for r in rows if tstart <= r["date"] < m + "-01"]
            if len(train) < 300:
                continue
            npm = fit_npred(train)
            pools = Pools(with_lp(train, npm))
            cache = {}
            for r in test:
                lp = predict_ln(npm, r)
                seg = "small" if math.exp(lp) < small else "big"
                got = pools.get(r["rng"], snap_key(lp))
                if got is None:
                    continue
                t = tal[seg]
                t["n"] += 1
                t["fair"] += 1 / (r["N"] + 1)
                xn = G0 + int(np.argmax(got[0])) * GS
                wn = r["S"] <= xn < r["W"]
                t["national"] += wn
                for lv in LOCAL_LEVELS:
                    key = (lv, r["rng"], seg)
                    if key not in cache:
                        cache[key] = _local_x(_local_pool(train, area, lv, r["rng"], seg, small))
                    x = cache[key] if cache[key] is not None else xn
                    wl = r["S"] <= x < r["W"]
                    t[lv] += wl
                    t[f"{lv}_b"] += wl and not wn   # 같은 공고에서 지역만 낙찰
                    t[f"{lv}_c"] += wn and not wl   # 전국만 낙찰
        segs, curves = {}, {lv: {} for lv in LOCAL_LEVELS}
        for seg, t in tal.items():
            # 채택(2026-09-27 강화): 같은 공고끼리 비교(엇갈린 공고만 셈)해 한쪽 부호검정 p < 0.05 ÷ 후보 수(시도·시군 × 규모 2 = 4),
            # 그리고 +10% 이상. 예전 기준(+2건·+10%)은 낙찰 7건 수준에서 우연으로도 자주 통과했다
            for lv in LOCAL_LEVELS:
                t[f"{lv}_p"] = _sign_p(int(t[f"{lv}_b"]), int(t[f"{lv}_c"]))
            best = min(LOCAL_LEVELS, key=lambda lv: (t[f"{lv}_p"], -t[lv]))
            use = best if t["n"] >= 50 and t[f"{best}_p"] < LOCAL_ALPHA and t[best] >= t["national"] * 1.1 else None
            segs[seg] = {"use": use, "val": {k: round(v, 4 if k.endswith("_p") else 2) for k, v in t.items()}}
            for lv in LOCAL_LEVELS:
                for rk in RNGS:
                    pool = _local_pool(recent, area, lv, rk, seg, small)
                    if len(pool) >= LOCAL_MIN:
                        sm = curve(np.array([r["S"] for r in pool]), np.array([r["W"] for r in pool]))
                        curves[lv].setdefault(seg, {})[rk] = summarize(sm, np.array([r["S"] for r in pool]), np.array([r["N"] for r in pool], float))
        out[f'{area["sido"]}|{area["sgg"]}'] = {"label": area.get("label") or area["sgg"], "small": small, "seg": segs, "curves": curves}
        print(f"local {area['sgg']}: " + " | ".join(f"{seg} 시험 {int(t['n'])} 전국 {int(t['national'])} 시도 {int(t['sido'])} 시군 {int(t['sgg'])} (p 시도 {t['sido_p']:.3f} 시군 {t['sgg_p']:.3f}) → {segs[seg]['use']}" for seg, t in tal.items()))
    return out


def main():
    rows = load_rows()
    if len(rows) < 300:
        print("model: 표본 부족, 건너뜀", len(rows))
        return
    last = rows[-1]["date"]
    cut = (dt.date.fromisoformat(last) - dt.timedelta(days=int(30.44 * TRAIN_MONTHS))).isoformat()
    recent = [r for r in rows if r["date"] >= cut]
    npred = fit_npred(recent)
    pools = Pools(with_lp(recent, npred))
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
        "band": round(math.exp(LN_BW), 2), "smooth": SMOOTH_K * GS, "amt_edges": AMT_EDGES,
        "meanS": {rk: round(float(np.mean(pools.by_rng[rk][0])), 4) for rk in RNGS if len(pools.by_rng[rk][0])},
        "npred": npred,
        "curves": curves,
        "validation": validate(rows),
        "local": local_models(rows, recent, npred),
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

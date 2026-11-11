"""로또 6/45 당첨번호 받기 + 분석 → data/draws.json, data/analysis.json
출처: 1) 동행복권 공식 API(국내에서만 열릴 때가 많음) 2) smok95/lotto 공개 JSON(GitHub). 둘 다 실패하면 기존 파일 유지.
분석: 시중 분석법(빈도·핫콜드·미출현·홀짝·고저·합계·연속·AC·끝수·소수·이월·동반 출현) + 표본외 역검증(각 방법으로 매 회차 직전까지만 보고 골랐다면 몇 개 맞았나)."""
import collections, datetime as dt, itertools, json, math, random, sys, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MIRROR = "https://raw.githubusercontent.com/smok95/lotto/main/results/all.json"
OFFICIAL = "https://www.dhlottery.co.kr/common.do?method=getLottoNumber&drwNo={}"
ALL = range(1, 46)
PRIMES = {2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43}


def get(url, timeout=30):
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def load_draws():
    old = json.load(open(DATA / "draws.json", encoding="utf-8")) if (DATA / "draws.json").exists() else []
    by = {d["no"]: d for d in old}
    try:
        for d in get(MIRROR, 60):
            n = sorted(int(x) for x in d["numbers"])
            dv = (d.get("divisions") or [{}])[0] or {}
            by[int(d["draw_no"])] = {"no": int(d["draw_no"]), "date": d["date"][:10], "n": n, "b": int(d["bonus_no"]),
                                     "w1": dv.get("winners"), "p1": dv.get("prize"), "sales": d.get("total_sales_amount")}
        print(f"공개 JSON {len(by)}회")
    except Exception as e:
        print("공개 JSON 실패:", type(e).__name__, e)
    # 공식 API로 다음 회차들 보충(열리면)
    nxt = max(by) + 1 if by else 1
    for _ in range(5):
        try:
            d = get(OFFICIAL.format(nxt), 15)
        except Exception as e:
            print("공식 API 안 열림:", type(e).__name__)
            break
        if d.get("returnValue") != "success":
            break
        by[nxt] = {"no": nxt, "date": d["drwNoDate"], "n": sorted(d[f"drwtNo{i}"] for i in range(1, 7)), "b": d["bnusNo"],
                   "w1": d.get("firstPrzwnerCo"), "p1": d.get("firstWinamnt"), "sales": d.get("totSellamnt")}
        nxt += 1
    draws = [by[k] for k in sorted(by)]
    for d in draws:   # 형식 점검
        assert len(set(d["n"])) == 6 and all(1 <= x <= 45 for x in d["n"]), d
    assert [d["no"] for d in draws] == list(range(1, len(draws) + 1)), "회차가 빠짐"
    return draws


def feats(s):
    s = sorted(s)
    diffs = {b - a for a, b in itertools.combinations(s, 2)}
    ends = collections.Counter(x % 10 for x in s)
    return {"odd": sum(x % 2 for x in s), "low": sum(x <= 22 for x in s), "sum": sum(s),
            "consec": sum(1 for a, b in zip(s, s[1:]) if b - a == 1), "ac": len(diffs) - 5,
            "same_end": max(ends.values()), "prime": sum(x in PRIMES for x in s)}


def sumbin(v):
    lo = max(60, min(200, v // 20 * 20))
    return "~79" if v < 80 else "200~" if v >= 200 else f"{lo}~{lo + 19}"


def dist(rows, key, fn=None):
    c = collections.Counter(fn(r[key]) if fn else r[key] for r in rows)
    t = sum(c.values())
    return {str(k): round(v / t * 100, 1) for k, v in sorted(c.items(), key=lambda x: (str(x[0]) if fn else x[0]))}


# ---------- 역검증용 고르기 방법 (직전 회차까지만 본다)
def counts(hist, k=None):
    return collections.Counter(x for d in (hist[-k:] if k else hist) for x in d["n"])


def m_hot(k):
    return lambda h, r: sorted(ALL, key=lambda n, c=counts(h, k): (-c[n], n))[:6]


def m_cold(k):
    return lambda h, r: sorted(ALL, key=lambda n, c=counts(h, k): (c[n], n))[:6]


def m_overdue(h, r):
    last = {}
    for i, d in enumerate(h):
        for x in d["n"]:
            last[x] = i
    return sorted(ALL, key=lambda n: (last.get(n, -1), n))[:6]


def ok_filter(s):
    f = feats(s)
    return 2 <= f["odd"] <= 4 and 2 <= f["low"] <= 4 and 100 <= f["sum"] <= 175 and f["consec"] <= 1 and f["ac"] >= 7


def m_filter(h, r):
    while True:
        s = r.sample(range(1, 46), 6)
        if ok_filter(s):
            return s


def m_carry(h, r):
    prev = r.sample(h[-1]["n"], 2)
    return prev + r.sample([x for x in ALL if x not in prev], 4)


def m_hotmix(h, r):   # 최근 30회 많이 나온 15개 중 무작위 6개
    c = counts(h, 30)
    return r.sample(sorted(ALL, key=lambda n: (-c[n], n))[:15], 6)


METHODS = [
    ("무작위(자동)", lambda h, r: r.sample(range(1, 46), 6), 20),
    ("전체 최다 출현 6개", m_hot(None), 1),
    ("최근 10회 핫 6개", m_hot(10), 1),
    ("최근 50회 핫 6개", m_hot(50), 1),
    ("최근 30회 핫 15개 중 무작위", m_hotmix, 20),
    ("최근 50회 콜드 6개", m_cold(50), 1),
    ("장기 미출현 6개", m_overdue, 1),
    ("필터 조합(홀짝·고저·합·연속·AC)", m_filter, 20),
    ("이월수 2개 + 무작위", m_carry, 20),
]
START = 100


def backtest(draws):
    N = len(draws)
    var = 6 * (6 / 45) * (39 / 45) * (39 / 44)   # 한 장 맞은 개수 분산(초기하)
    res = []
    for name, f, reps in METHODS:
        rng = random.Random(7)
        hits = collections.Counter()
        tot = 0
        for _ in range(reps):
            for i in range(START, N):
                hits[len(set(f(draws[:i], rng)) & set(draws[i]["n"]))] += 1
                tot += 1
        mean = sum(k * v for k, v in hits.items()) / tot
        se = math.sqrt(var / (N - START))   # 같은 회차를 여러 번 돌려도 독립 표본은 회차 수만큼
        res.append({"name": name, "tickets": tot, "draws": N - START, "mean": round(mean, 4),
                    "ci": [round(mean - 1.96 * se, 4), round(mean + 1.96 * se, 4)],
                    "ge3": round(sum(v for k, v in hits.items() if k >= 3) / tot * 100, 3),
                    "dist": [hits[k] for k in range(7)]})
    p3 = sum(math.comb(6, k) * math.comb(39, 6 - k) for k in range(3, 7)) / math.comb(45, 6)
    return {"from": START + 1, "theory_mean": 0.8, "theory_ge3": round(p3 * 100, 3), "methods": res}


def crowd(draws):
    """1등 당첨자 수 ÷ 무작위 기대(판매 장수 ÷ 8,145,060): 사람들이 많이 고르는 번호 모양이면 1등을 나눠 갖는다"""
    rows = [d for d in draws if d.get("w1") is not None and d.get("sales")]
    out = {}
    for label, key in [("low31", lambda n: sum(x <= 31 for x in n)),
                       ("consec", lambda n: min(2, sum(1 for a, b in zip(n, n[1:]) if b - a == 1))),
                       ("max40", lambda n: "40 이상" if n[-1] >= 40 else "39 이하")]:
        g = collections.defaultdict(lambda: [0, 0.0, 0])
        for d in rows:
            k = str(key(d["n"]))
            g[k][0] += d["w1"]
            g[k][1] += d["sales"] / 1000 / 8145060
            g[k][2] += 1
        out[label] = {k: {"draws": c, "winners": w, "expect": round(e, 1)} for k, (w, e, c) in sorted(g.items())}
    return {"draws": len(rows), "groups": out}


def analyze(draws):
    N = len(draws)
    cnt = counts(draws)
    exp = N * 6 / 45
    gap = {}
    for n in ALL:
        g = 0
        for d in reversed(draws):
            if n in d["n"]:
                break
            g += 1
        gap[n] = g
    rng = random.Random(1)
    sim = [feats(rng.sample(range(1, 46), 6)) for _ in range(200000)]
    real = [feats(d["n"]) for d in draws]
    pats = {k: {"real": dist(real, k, fn), "random": dist(sim, k, fn)}
            for k, fn in [("odd", None), ("low", None), ("sum", sumbin), ("consec", None), ("ac", None), ("same_end", None), ("prime", None)]}
    carry = collections.Counter(len(set(draws[i]["n"]) & set(draws[i - 1]["n"])) for i in range(1, N))
    pairs = collections.Counter(p for d in draws for p in itertools.combinations(d["n"], 2))
    filt_share = sum(ok_filter(rng.sample(range(1, 46), 6)) for _ in range(100000)) / 1000
    return {
        "updated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="minutes"),
        "n_draws": N, "last": draws[-1],
        "freq": [cnt[n] for n in ALL], "freq_expect": round(exp, 1),
        "chi2": round(sum((cnt[n] - exp) ** 2 / exp for n in ALL), 1), "chi2_crit": 60.48,
        "recent10": [counts(draws, 10)[n] for n in ALL], "recent50": [counts(draws, 50)[n] for n in ALL],
        "gap": [gap[n] for n in ALL],
        "patterns": pats,
        "carry": {str(k): round(v / (N - 1) * 100, 1) for k, v in sorted(carry.items())},
        "pairs_top": [[list(p), c] for p, c in pairs.most_common(10)], "pair_expect": round(N * 15 / 990, 1),
        "filter_share": round(filt_share, 1),
        "backtest": backtest(draws),
        "crowd": crowd(draws),
    }


def main():
    DATA.mkdir(exist_ok=True)
    draws = load_draws()
    old = json.load(open(DATA / "draws.json", encoding="utf-8")) if (DATA / "draws.json").exists() else []
    if old and len(old) == len(draws) and "--force" not in sys.argv:
        print("새 회차 없음")
        return
    json.dump(draws, open(DATA / "draws.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    json.dump(analyze(draws), open(DATA / "analysis.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"{len(draws)}회 저장 (마지막 {draws[-1]['no']}회 {draws[-1]['date']})")


if __name__ == "__main__":
    main()

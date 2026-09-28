"""물품 '추첨 평균' 낙찰확률 곡선 — 공고마다 복수예가 15개 중 4개를 고르는 1,365가지 경우를 모두 따져
'이 투찰 사정률 x 로 넣었으면 1순위였을 확률'을 구하고, 달 × 참가 규모 × 예가범위 × 계약방법별로 더해 둔다 (2026-09-28).

왜: 참가 수백~수천 곳 물품은 1순위 자리가 사정율 바로 위 0.001%p 안팎이라, 실제로 일어난 한 번의 추첨으로 세면
공고 1,000건에 1순위가 1건 나올까 말까 — 어떤 금액이 나은지 셀 수가 없다. 추첨 경우를 평균하면 같은 공고로 흔들림이 훨씬 작다.
국방 물품 2026-02~09 3,052건으로 먼저 확인: 실제 추첨으로 센 1순위와 계산 기대가 맞음(참가 50곳↑ 9 vs 7.8).

원자료(전원 투찰 금액)는 전국 1년이면 수백 MB라 저장하지 않고, 계산한 합계만 data/thng_curve.json 에 남긴다:
{"v":1, "grid":{"x0":97,"step":0.01,"n":701}, "done":{공고ID:1}, "fail":{공고ID:횟수},
 "agg":{"YYYY-MM|규모|예가범위|수의·경쟁": {"n":공고 수, "fair":Σ1/(참가+1), "c":[칸마다 1순위 확률 합], "rc":[실제 추첨으로 x 가 [S,1순위) 안이던 공고 수]}}}
규모 = 50~299 / 300~999 / 1000+ (실제 참가 수). 우리 업체(WATCH_BIZ) 행은 경쟁자에서 뺀다.
"""
import itertools
import json
from pathlib import Path

import numpy as np

X0, STEP, NG = 97.0, 0.01, 701
GRID = X0 + STEP * np.arange(NG)
_COMB = {}


def comb(n):
    if n not in _COMB:
        _COMB[n] = np.array(list(itertools.combinations(range(n), 4)))
    return _COMB[n]


def bucket(n):
    return "50~299" if n < 300 else "300~999" if n < 1000 else "1000+"


def lottery_curve(prices, amts, base, floor):
    """prices: 복수예가 목록, amts: 경쟁 업체 유효 후보 투찰금액(순위 있음 + 하한 미달), base: 기초금액, floor: 하한율(%)
    → 칸마다 1순위 확률(np.float64[NG]), 없으면 None"""
    if len(prices) < 4 or len(amts) < 1 or not base or not floor:
        return None
    P = np.asarray(prices, float)
    plan_c = P[comb(len(P))].mean(1)
    F = plan_c * floor / 100                       # 추첨마다 낙찰하한가
    A = np.sort(np.asarray(amts, float))
    i = np.searchsorted(A, F)                      # 하한가 이상 중 가장 낮은 경쟁 투찰 = 1순위
    W = np.where(i < len(A), A[np.minimum(i, len(A) - 1)], np.inf)
    unit = base * floor / 100 / 100                # 금액 ÷ unit = 투찰 사정률
    d = np.zeros(NG + 1)
    np.add.at(d, np.clip(np.searchsorted(GRID, F / unit - 1e-9), 0, NG), 1)
    np.add.at(d, np.clip(np.searchsorted(GRID, W / unit - 1e-9), 0, NG), -1)
    return np.cumsum(d)[:-1] / len(plan_c)


def real_window(plan, top_amt, base, floor):
    """실제 추첨 결과의 [S, 1순위) → 칸별 0/1"""
    if not (plan and top_amt and base and floor):
        return None
    unit = base * floor / 100 / 100
    s, w = plan * floor / 100 / unit, top_amt / unit
    return (GRID >= s - 1e-9) & (GRID < w - 1e-9)


class ThngCurve:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.d = json.load(open(path, encoding="utf-8"))
        except Exception:
            self.d = {}
        self.d.setdefault("v", 1)
        self.d["grid"] = {"x0": X0, "step": STEP, "n": NG}
        self.d.setdefault("done", {})
        self.d.setdefault("fail", {})
        self.d.setdefault("agg", {})
        self.dirty = False

    def add(self, key, n, curve, real):
        a = self.d["agg"].setdefault(key, {"n": 0, "fair": 0.0, "c": [0.0] * NG, "rc": [0] * NG})
        a["n"] += 1
        a["fair"] = round(a["fair"] + 1 / (n + 1), 6)
        a["c"] = [round(x, 4) for x in (np.asarray(a["c"]) + curve)]
        if real is not None:
            a["rc"] = (np.asarray(a["rc"]) + real.astype(int)).tolist()
        self.dirty = True

    def save(self):
        if not self.dirty:
            return
        self.path.write_text(json.dumps(self.d, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        self.dirty = False

#!/usr/bin/env python3
"""모의 투찰(실시간 검증): 진행중 공고마다 마감 전 앱과 같은 추천 투찰가를 기록해 두고, 개찰 뒤 실제 결과로 채점한다.

- 대상: PAPER_SIDO(기본 강원) 공사 공고 중 기초금액이 공개된 것. 앱에서 춘천/강원 전체로 나눠 본다.
- 기록: 마감 전에는 실행할 때마다 그때의 모델 추천으로 덮어쓴다(= 앱이 마감 직전에 보여 준 값). 마감 뒤에는 고정.
- 추천 계산은 앱 modelPredict 와 같다: 참가수 예측(model.predict_ln) → 예가범위·예상 참가 곡선(가장 가까운 k) → 지역 전용(local, use 일 때) → x.
- 채점: 개찰 상세(data/opening)가 있으면 실제 전체 투찰로 — 낙찰하한가 L 이상이고 L 이상 투찰 중 가장 낮으면 1순위, 순위도.
  없으면 낙찰 목록(data/scsbid)의 예정가격·1위 금액으로 S ≤ x < W 판정.
- 비교: 평균 사정율로 넣었다면(xm), 공정 기대 1/(참가+1).
→ data/paper.json {"v":1, "updated_at", "sido", "items":[{id, nm, org, sgg, base, a, floor, rng, close, open, x, bid, xm, n(예상 참가), at, area?, res?}]}
   res = {S, W?, cnt, win(0/1), below(0/1), rank?, winner, mwin(평균 사정율 방식 낙찰 0/1), src:'detail'|'list'}
"""
import datetime as dt
import glob
import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from model import amt_bin, bid_to_sr, floor_key, predict_ln, rgn_scope, rng_key  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
KST = dt.timezone(dt.timedelta(hours=9))
SIDO = os.environ.get("PAPER_SIDO") or "강원"
DEFAULT_FLOOR = 87.745


def load(p, d):
    try:
        return json.load(open(p, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return d


def parse_kst(s):
    try:
        return dt.datetime.strptime(s[:16], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    except (TypeError, ValueError):
        return None


def js_round(v):
    return math.floor(v + 0.5)


def bid_amount(base, x, a, floor):
    return math.ceil((base * x / 100 - a) * floor / 100 + a)


def recommend(model, b):
    """앱 modelPredict 와 같은 추천 → (x, 예상 참가, 평균 사정율, 지역 라벨)"""
    rk = rng_key(b.get("rng"))
    curves = model["curves"]
    use_rk = rk if rk in curves else "-3,3"
    row = {"ab": amt_bin(b["base"]), "rng": rk, "sido": b.get("sido") or "", "sgg": b.get("sgg") or "",
           "fl": floor_key(b.get("floor")), "lic": "+".join(sorted(b.get("lic") or [])),
           "org": b.get("org") or b.get("dmd") or "", "sc": rgn_scope(b.get("rgn"))}
    ln = predict_ln(model["npred"], row)
    k = js_round(ln * 5) * 2
    lst = curves[use_rk]
    e = min(lst, key=lambda c: abs(c["k"] - k))   # 같은 거리면 앞쪽(앱 reduce 와 같음)
    area = None
    L = (model.get("local") or {}).get(f'{row["sido"]}|{row["sgg"]}')
    if L:
        seg = "small" if math.exp(ln) < L["small"] else "big"
        S = (L.get("seg") or {}).get(seg) or {}
        e2 = S.get("use") and (((L.get("curves") or {}).get(S["use"]) or {}).get(seg) or {}).get(use_rk)
        if e2:
            e, area = e2, L.get("label")
    return e["x"], max(1, js_round(math.exp(ln))), model["meanS"].get(use_rk), area


def load_results():
    """id → 낙찰 목록 레코드, id → 개찰 상세"""
    recs = {}
    for f in glob.glob(str(DATA / "scsbid" / f"{SIDO}*.json")):
        for r in load(f, {}).get("items", []):
            recs[r["id"]] = r
    ops = {}
    for f in glob.glob(str(DATA / "opening" / SIDO / "[0-9]*.json")):
        d = load(f, {})
        for i, b in (d.get("bids") or {}).items():
            ops[i] = b
    return recs, ops


def score(it, rec, op):
    base = (op or {}).get("base") or rec.get("base") or it["base"]
    plan = (op or {}).get("plan") or rec.get("plan")
    a, floor = it.get("a") or 0, it.get("floor") or DEFAULT_FLOOR
    if not (base and plan):
        return None
    S = plan / base * 100
    x = bid_to_sr(it["bid"], base, a, floor) or it["x"]
    xm = it.get("xm")
    res = {"S": round(S, 4), "cnt": rec.get("cnt") or (len(op["r"]) if op and op.get("r") else None), "winner": rec.get("win")}
    if op and op.get("r"):
        L = math.ceil((plan - a) * floor / 100 + a)
        valid = sorted(r[2] for r in op["r"] if r[2] and r[2] >= L)
        B = it["bid"]
        res.update(src="detail", below=int(B < L))
        if B >= L:
            lower = sum(1 for v in valid if v < B)
            res["rank"] = lower + 1
            res["win"] = int(lower == 0)
        else:
            res["win"] = 0
        if xm:
            Bm = bid_amount(base, xm, a, floor)
            res["mwin"] = int(Bm >= L and not any(v < Bm for v in valid))
        res["cnt"] = res["cnt"] or len(op["r"])
    else:
        amt = rec.get("amt")
        W = bid_to_sr(amt, base, a, floor) if amt else None
        if W is None:
            return None
        res.update(src="list", W=round(W, 4), below=int(x < S), win=int(S <= x < W))
        if xm:
            res["mwin"] = int(S <= xm < W)
    return res


def main():
    now = dt.datetime.now(KST)
    model = load(DATA / "model.json", None)
    if not model or "npred" not in model:
        print("model.json 없음 — 건너뜀")
        return
    bids = load(DATA / "bids.json", {}).get("items", [])
    out = load(DATA / "paper.json", {"v": 1, "items": []})
    items = {it["id"]: it for it in out.get("items", [])}
    # ① 마감 전 공고: 지금 추천으로 기록(덮어쓰기)
    n_new = 0
    for b in bids:
        if b.get("sido") != SIDO or not b.get("base"):
            continue
        close = parse_kst(b.get("close") or "")
        if close and close <= now:
            continue
        try:
            x, nexp, xm, area = recommend(model, b)
        except (KeyError, ValueError, ZeroDivisionError) as e:
            print("추천 실패", b.get("id"), e)
            continue
        a, floor = b.get("a") or 0, b.get("floor") or DEFAULT_FLOOR
        it = {"id": b["id"], "nm": b.get("nm"), "org": b.get("org") or b.get("dmd"), "sgg": b.get("sgg"),
              "base": b["base"], "a": b.get("a"), "floor": b.get("floor"), "rng": b.get("rng"),
              "close": b.get("close"), "open": b.get("open"), "x": x, "bid": bid_amount(b["base"], x, a, floor),
              "xm": xm, "n": nexp, "at": now.isoformat(timespec="minutes")}
        if area:
            it["area"] = area
        old = items.get(b["id"])
        if not old:
            n_new += 1
        elif old.get("bid") == it["bid"] and old.get("x") == x:
            it["at"] = old.get("at")   # 추천이 그대로면 기록 시각도 그대로(파일이 괜히 바뀌지 않게)
        items[b["id"]] = {k: v for k, v in it.items() if v not in (None, "", [])}
    # ② 개찰 지난 기록: 결과로 채점 (상세가 나중에 들어오면 다시 채점)
    recs, ops = load_results()
    n_scored = 0
    for it in items.values():
        if it.get("res", {}).get("src") == "detail":
            continue
        rec, op = recs.get(it["id"]), ops.get(it["id"])
        if not rec and not op:
            continue
        r = score(it, rec or {}, op)
        if r:
            it["res"] = {k: v for k, v in r.items() if v is not None}
            n_scored += 1
    lst = sorted(items.values(), key=lambda it: (it.get("open") or it.get("close") or ""), reverse=True)
    body = {"v": 1, "sido": SIDO, "updated_at": now.isoformat(timespec="seconds"), "items": lst}
    text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    old = load(DATA / "paper.json", None)
    if old is None or json.dumps({**old, "updated_at": ""}, ensure_ascii=False, separators=(",", ":")) != json.dumps({**body, "updated_at": ""}, ensure_ascii=False, separators=(",", ":")):
        (DATA / "paper.json").write_text(text, encoding="utf-8")
    done = [it for it in lst if it.get("res")]
    print(f"모의 투찰: 기록 {len(lst)} (새 {n_new}) · 채점 {len(done)} (이번 {n_scored}) · 낙찰 {sum(it['res'].get('win', 0) for it in done)}")


if __name__ == "__main__":
    main()

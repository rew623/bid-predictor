#!/usr/bin/env python3
"""물품 모의 투찰(2026-09-29 요청): 진행중 물품 공고(나라장터 goods.json·국방 d2b/bids.json)마다 마감 전 앱과 같은 추천 투찰가를
기록해 두고(data/thng_rec.json 의 x — 마감 뒤엔 고정), 개찰 뒤 실제 결과로 채점한다. 🧪 모의 투찰의 '📦 물품'.

- 기록: 기초금액·하한율이 공개된 공고. 마감 전에는 실행할 때마다 그때의 추천으로 덮어쓰고, 마감 뒤에는 그대로 둔다(= 개찰 전 값).
  나라장터는 참가 50곳 미만용 위치(small_x, 101.0)도 같이 기록해 둘 다 채점.
- 채점(낙찰 목록 기준 근사, 공사 모의 투찰의 'list' 와 같음): S = 예정가격 ÷ 기초금액, W = 1순위(국방은 개찰 순위 1위, 나라장터는 최종 낙찰) 금액의 투찰 사정률
  → S ≤ x < W 면 1순위, x < S 면 하한 미달. 공정 기대 = 1/(참가+1).
  나라장터 결과 = data/thng/{시도}.json(같은 공고 id), 국방 결과 = data/d2b/{연도}/{월}.json(연도·기관코드·판단번호로 맞춤 — 공고 id 와 결과 id 가 다름).
- 크기: 개찰 뒤 KEEP_DAYS(180)일 지난 기록은 뺀다(나라장터 물품은 하루 100건 안팎).
→ data/paper_thng.json {"v":1, "updated_at", "items":[{id, src:'나라장터'|'국방', nm, org, base, floor, close, open, x, bid, xs?, bids?, at, res?}]}
   res = {S, W, cnt, win, below, wins?(small_x 로 넣었다면 1순위), amt(1순위·낙찰 금액), winner}
"""
import datetime as dt
import glob
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
KST = dt.timezone(dt.timedelta(hours=9))
KEEP_DAYS = 180


def load(p, d):
    try:
        return json.load(open(p, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return d


def parse_kst(s):
    try:
        return dt.datetime.strptime(str(s)[:16], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    except (TypeError, ValueError):
        return None


def bid_amount(base, x, floor):
    return math.ceil(base * x / 100 * floor / 100)


def d2b_key(id_):
    """국방 공고·결과 id → (연도, 기관코드, 판단번호). DB-2026-LCM-37805-LCM0045 / D-2026-UMM-38314-***-UMM0724-1"""
    p = str(id_).split("-")
    if len(p) >= 4 and p[1].isdigit():
        return p[1], p[2], p[3]
    return None


def results():
    g2b = {}
    for f in glob.glob(str(DATA / "thng" / "*.json")):
        for r in load(f, {}).get("items", []):
            g2b[r["id"]] = r
    d2b = {}
    for f in glob.glob(str(DATA / "d2b" / "20*" / "*.json")):
        for r in load(f, {}).get("items", []):
            if r.get("kind") == "물품":
                k = d2b_key(r["id"])
                if k:
                    d2b[k] = r
    return g2b, d2b


def score(it, rec):
    base, floor = rec.get("base") or it["base"], it.get("floor") or rec.get("floor")
    plan = rec.get("plan")
    if not (base and plan and floor):
        return None
    unit = base * floor / 100 / 100
    S = plan / base * 100
    if rec.get("x"):
        # 국방: 전원 투찰률(예정가격 대비 %)로 — 하한율 이상 중 가장 낮은 투찰이 1순위. 1위 금액을 기초금액·하한율로 되돌리면
        # 절반가량이 S 아래로 나와(국방 예정가격·하한 적용 방식 차이로 보임) 금액 대신 투찰률로 비교한다
        rates = [0.0] * len(rec["x"])
        acc = 0
        for i, d in enumerate(rec["x"]):
            acc += d
            rates[i] = acc / 1000
        valid = [r for r in rates if r >= floor]
        mine = it["bid"] / plan * 100
        res = {"S": round(S, 4), "cnt": rec.get("cnt") or len(rates), "below": int(mine < floor), "winner": rec.get("win"),
               "amt": round(min(valid) / 100 * plan) if valid else rec.get("amt")}
        res["win"] = int(mine >= floor and (not valid or mine < min(valid)))
        if it.get("bids"):
            ms = it["bids"] / plan * 100
            res["wins"] = int(ms >= floor and (not valid or ms < min(valid)))
        return {k: v for k, v in res.items() if v is not None}
    top = next((r for r in rec.get("r") or [] if r[0] == 1), None)
    w_amt = top[2] if top else rec.get("amt")
    if not w_amt:
        return None
    W = w_amt / unit
    if W < S - 1e-6:
        return {"na": 1}   # 1위 금액이 하한 아래로 계산됨(하한율·예정가격 정보가 어긋남) — 채점에서 뺌
    x = it["bid"] / unit
    res = {"S": round(S, 4), "W": round(W, 4), "cnt": rec.get("cnt"), "win": int(S <= x < W), "below": int(x < S),
           "amt": w_amt, "winner": rec.get("win")}
    if it.get("bids"):
        xs = it["bids"] / unit
        res["wins"] = int(S <= xs < W)
    return {k: v for k, v in res.items() if v is not None}


def main():
    now = dt.datetime.now(KST)
    rec = load(DATA / "thng_rec.json", {})
    out = load(DATA / "paper_thng.json", {"v": 1, "items": []})
    items = {it["id"]: it for it in out.get("items", [])}
    n_new = 0
    sources = [("나라장터", rec.get("g2b"), load(DATA / "goods.json", {}).get("items", [])),
               ("국방", rec.get("d2b"), [b for b in load(DATA / "d2b" / "bids.json", {}).get("items", []) if b.get("kind") == "물품"])]
    for src, r, notices in sources:
        if not r or not r.get("x"):
            continue
        for b in notices:
            if not (b.get("base") and b.get("floor")):
                continue
            close = parse_kst(b.get("close"))
            if not close or close <= now:
                continue
            it = {"id": b["id"], "src": src, "nm": b.get("nm"), "org": b.get("dmd") or b.get("org"), "base": b["base"], "floor": b["floor"],
                  "close": b.get("close"), "open": b.get("open"), "x": r["x"], "bid": bid_amount(b["base"], r["x"], b["floor"]),
                  "at": now.isoformat(timespec="minutes")}
            if r.get("small_x"):
                it["xs"] = r["small_x"]
                it["bids"] = bid_amount(b["base"], r["small_x"], b["floor"])
            old = items.get(b["id"])
            if not old:
                n_new += 1
            elif old.get("bid") == it["bid"]:
                it["at"] = old.get("at")
            items[b["id"]] = {k: v for k, v in it.items() if v not in (None, "")}
    g2b, d2b = results()
    n_scored = 0
    cut = (now - dt.timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    for it in list(items.values()):
        if (it.get("open") or it.get("close") or "9999") < cut:
            items.pop(it["id"])
            continue
        if it.get("res"):
            continue
        r = g2b.get(it["id"]) if it["src"] == "나라장터" else d2b.get(d2b_key(it["id"]))
        if not r:
            continue
        s = score(it, r)
        if s:
            it["res"] = s
            n_scored += 1
    lst = sorted(items.values(), key=lambda it: (it.get("open") or it.get("close") or ""), reverse=True)
    body = {"v": 1, "updated_at": now.isoformat(timespec="seconds"), "items": lst}
    old = load(DATA / "paper_thng.json", None)
    if old is None or old.get("items") != lst:
        (DATA / "paper_thng.json").write_text(json.dumps(body, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    done = [it for it in lst if it.get("res")]
    print(f"물품 모의 투찰: 기록 {len(lst)} (새 {n_new}) · 채점 {len(done)} (이번 {n_scored}) · 1순위 {sum(it['res'].get('win', 0) for it in done)}")


if __name__ == "__main__":
    main()

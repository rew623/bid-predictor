"""우리 업체 투찰만 뽑은 작은 파일 → 앱 내 투찰 '🏁 개찰 결과' 자동 찾기 (2026-09-27 요청 '국방·물품도 끌어와')

앱이 국방 월 파일(수십 MB)·물품 개찰 순위·다른 시·도 개찰 상세를 직접 읽으면 느려서, 수집 뒤 여기서 우리 행만 골라 둔다.
  python scripts/my_bids.py g2b  → data/mine.json      (나라장터: 강원 밖 시·도 개찰 상세 + 물품 개찰 순위 opening_thng) — 수집 잡
  python scripts/my_bids.py d2b  → data/d2b/mine.json  (국방 월 파일) — 국방 잡 (두 잡이 따로 커밋하므로 파일을 나눔)
우리 업체 = 환경변수 WATCH_BIZ(시크릿). 파일에는 사업자번호를 넣지 않는다(공개 저장소).
강원(전원 행이 있는 시·도)은 앱이 개찰 상세를 직접 읽으므로 뺀다.

형식: {"v":1, "items":[{id, src:"나라장터"|"국방", kind:"공사"|"물품"|"용역", nm, org, dmd?, sido?, sgg?, lic?, base, a?, floor?, rng?,
        date, plan, n(참가 수), top:[[순위, 업체명, 금액, 투찰률, 비고?]…10], mine:[순위(0=순위 없음), 금액, 투찰률, 비고?, 추정(1=금액을 투찰률로 계산)?],
        p?:[[번호, 복수예가, 추첨 0/1]…15]}]}
+ 전원 투찰률은 따로 data/mine_x.json · data/d2b/mine_x.json {공고ID: [투찰률×1000, 앞 값과의 차이…](순위 순)} — 앱이 그래프를 열 때만 읽음(2026-09-28 '우리가 넣은 공고는 상세 데이터 다')
"""
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
WATCH = {re.sub(r"\D", "", b) for b in (os.environ.get("WATCH_BIZ") or "").split(",") if re.sub(r"\D", "", b)}
try:
    FULL_SIDOS = set(json.load(open(ROOT / "scripts" / "detail_priority.json", encoding="utf-8")).get("sido") or ["강원"])
except Exception:
    FULL_SIDOS = {"강원"}


def load(p, default=None):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return default


def save(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if p.exists() and p.read_text(encoding="utf-8") == s:
        return
    p.write_text(s, encoding="utf-8")


XS = {}   # 공고ID → 전원 투찰률(순위 순, ×1000 차이)


def ours(corps):
    return {i for i, c in enumerate(corps) if len(c) > 1 and c[1] in WATCH}


def mine_row(b, corps, mi, has_rate):
    """우리 행: r(금액 행)에 있으면 그대로, 없으면 전원 목록 c 의 자리·투찰률(x 누적)로 — 금액은 투찰률 × 예정가격(추정)"""
    for row in b.get("r") or []:
        if row[1] in mi:
            if has_rate:   # 나라장터 [순위, 업체, 금액, 투찰률, 비고?]
                return [row[0], row[2], row[3]] + ([row[4]] if len(row) > 4 else [])
            rate = round(row[2] / b["plan"] * 100, 3) if b.get("plan") else None   # 국방 [순위, 업체, 금액, 비고?]
            return [row[0], row[2], rate] + ([row[3]] if len(row) > 3 else [])
    c = b.get("c") or []
    for i, ci in enumerate(c):
        if ci in mi:
            xs = b.get("x") or []
            rate = sum(xs[:i + 1]) / 1000 if len(xs) > i else None
            amt = round(rate / 100 * b["plan"]) if rate and b.get("plan") else None
            return [i + 1 if i < (b.get("k") or 0) else 0, amt, rate, "", 1]
    return None


def top10(b, corps, has_rate):
    out = []
    for row in (b.get("r") or [])[:10]:
        name = corps[row[1]][0] if row[1] < len(corps) else ""
        if has_rate:
            out.append([row[0], name, row[2], row[3]] + ([row[4]] if len(row) > 4 else []))
        else:
            rate = round(row[2] / b["plan"] * 100, 3) if b.get("plan") else None
            out.append([row[0], name, row[2], rate] + ([row[3]] if len(row) > 3 else []))
    return out


def g2b():
    meta = load(DATA / "meta.json", {})
    files = meta.get("files") or {}
    items = []
    for kind, key, recs_key in (("공사", "opening", "scsbid"), ("물품", "opening_thng", "thng")):
        for sido, years in (files.get(key) or {}).items():
            if kind == "공사" and sido in FULL_SIDOS:
                continue
            recs = None
            for paths in years.values():
                for path in paths:
                    y = load(DATA / path, {})
                    corps = y.get("corps") or []
                    mi = ours(corps)
                    if not mi:
                        continue
                    for bid_id, b in (y.get("bids") or {}).items():
                        m = mine_row(b, corps, mi, True)
                        if not m:
                            continue
                        if recs is None:   # 공고 정보(이름·기관·면허·하한율…)는 낙찰 기록에서 — 우리 투찰이 있는 시·도만 읽음
                            recs = {}
                            for p in (files.get(recs_key) or {}).get(sido) or []:
                                for r in (load(DATA / p, {}) or {}).get("items") or []:
                                    recs[r["id"]] = r
                        rec = recs.get(bid_id, {})
                        it = {"id": bid_id, "src": "나라장터", "kind": kind, "nm": rec.get("nm") or bid_id, "org": rec.get("org"), "dmd": rec.get("dmd"),
                              "sido": rec.get("sido") or sido, "sgg": rec.get("sgg"), "lic": rec.get("lic"), "base": b.get("base") or rec.get("base"),
                              "a": rec.get("a"), "floor": rec.get("floor"), "rng": rec.get("rng"), "date": b.get("date") or rec.get("date"),
                              "plan": b.get("plan") or rec.get("plan"), "n": b.get("n") or len(b.get("c") or []) or len(b.get("r") or []) or rec.get("cnt"),
                              "top": top10(b, corps, True), "mine": m, "p": [q[:3] for q in b.get("p") or []]}
                        items.append({k: v for k, v in it.items() if v not in (None, "", [])})
                        if b.get("x"):
                            XS[bid_id] = b["x"]
    return items


def d2b():
    corps = (load(DATA / "d2b" / "corps.json", {}) or {}).get("corps") or []
    mi = ours(corps)
    items = []
    if not mi:
        return items
    kinds = {"시설": "공사"}
    for p in sorted((DATA / "d2b").glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]*.json")):
        for b in (load(p, {}) or {}).get("items") or []:
            if not b.get("plan") and b.get("r") and b.get("x") and b["x"][0]:   # 예정가격이 빈 공고: 1위 금액 ÷ 1위 투찰률(추정)
                b["plan"] = round(b["r"][0][2] / (b["x"][0] / 100000))
            m = mine_row(b, corps, mi, False)
            if not m:
                continue
            it = {"id": b["id"], "src": "국방", "kind": kinds.get(b.get("kind"), b.get("kind") or "물품"), "nm": b.get("nm") or b["id"], "org": b.get("org"),
                  "base": b.get("base"), "floor": b.get("floor"), "rng": b.get("rng"), "date": b.get("date"), "plan": b.get("plan"),
                  "n": b.get("cnt") or len(b.get("c") or []), "top": top10(b, corps, False), "mine": m, "cm": b.get("cm"),
                  "p": [q[:3] for q in b.get("p") or []]}
            items.append({k: v for k, v in it.items() if v not in (None, "", [])})
            if b.get("x"):
                XS[b["id"]] = b["x"]
    return items


def main():
    which = (sys.argv[1:] or ["g2b"])[0]
    if not WATCH:
        print("WATCH_BIZ 없음 — 건너뜀")
        return
    items = d2b() if which == "d2b" else g2b()
    items.sort(key=lambda x: x.get("date") or "", reverse=True)
    out = DATA / "d2b" / "mine.json" if which == "d2b" else DATA / "mine.json"
    save(out, {"v": 1, "items": items})
    save(out.with_name("mine_x.json"), XS)
    print(f"[우리 투찰] {which} {len(items)}건 → {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

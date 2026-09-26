#!/usr/bin/env python3
"""업체 색인: 업체 검색용 → data/corps.json (수집 뒤 워크플로에서 실행)

출처: 전국 낙찰(data/scsbid 의 win·winBiz) + 개찰 상세(data/opening, regions.json 지역의 전체 투찰) + 국방(data/d2b 상위 30곳).
{"v":1, "updated_at", "sidos":[시도…], "items":[[사업자번호, 업체명, 전국 낙찰 수, 마지막 낙찰일, 낙찰 시도 번호들, 상세 투찰 수, 상세 1순위 수, 국방 투찰 수, 국방 1순위 수], …]}
업체별 투찰 내역(금액·순위)은 앱이 개찰 상세 파일을 직접 읽어 계산한다.
"""
import collections
import datetime as dt
import glob
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
KST = dt.timezone(dt.timedelta(hours=9))


def load(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def main():
    # 3MB 파일이라 커밋마다 저장소가 커진다 → 하루 한 번만 (FORCE=1 이면 바로)
    old_at = load(DATA / "corps.json").get("updated_at")
    if old_at and not os.environ.get("FORCE"):
        age = dt.datetime.now(KST) - dt.datetime.fromisoformat(old_at)
        if age < dt.timedelta(hours=20):
            print(f"업체 색인: {age.seconds // 3600}시간 전에 만듦 — 건너뜀")
            return
    name = {}
    wins = collections.Counter()
    last = {}
    wsido = collections.defaultdict(collections.Counter)
    sidos = []
    for f in sorted(glob.glob(str(DATA / "scsbid" / "*.json"))):
        for r in load(f).get("items", []):
            b = r.get("winBiz")
            if not b:
                continue
            wins[b] += 1
            name.setdefault(b, r.get("win") or "")
            if (r.get("date") or "") > last.get(b, ""):
                last[b] = r.get("date") or ""
                name[b] = r.get("win") or name[b]
            s = r.get("sido") or ""
            if s:
                if s not in sidos:
                    sidos.append(s)
                wsido[b][sidos.index(s)] += 1
    dbid, dtop = collections.Counter(), collections.Counter()
    for f in glob.glob(str(DATA / "opening" / "*" / "[0-9]*.json")):
        d = load(f)
        corps = d.get("corps") or []
        for b in (d.get("bids") or {}).values():
            for row in b.get("r") or []:
                nm, biz = corps[row[1]]
                if not biz:
                    continue
                dbid[biz] += 1
                if row[0] == 1:
                    dtop[biz] += 1
                name.setdefault(biz, nm)
    mbid, mtop = collections.Counter(), collections.Counter()
    for f in glob.glob(str(DATA / "d2b" / "[0-9]*.json")):
        d = load(f)
        corps = d.get("corps") or []
        for it in d.get("items") or []:
            for row in it.get("r") or []:
                nm, biz = corps[row[1]]
                if not biz:
                    continue
                mbid[biz] += 1
                if row[0] == 1:
                    mtop[biz] += 1
                name.setdefault(biz, nm)
    items = []
    for b, nm in name.items():
        items.append([b, nm, wins[b], last.get(b, ""), [k for k, _ in wsido[b].most_common(3)],
                      dbid[b], dtop[b], mbid[b], mtop[b]])
    items.sort(key=lambda x: (-(x[2] + x[5]), x[1]))
    body = {"v": 1, "updated_at": dt.datetime.now(KST).isoformat(timespec="seconds"), "sidos": sidos, "items": items}
    text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    old = load(DATA / "corps.json")
    if old.get("items") != items or old.get("sidos") != sidos:
        (DATA / "corps.json").write_text(text, encoding="utf-8")
    print(f"업체 색인 {len(items)}곳 ({len(text) // 1024}KB)")


if __name__ == "__main__":
    main()

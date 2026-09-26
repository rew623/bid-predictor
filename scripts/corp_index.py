#!/usr/bin/env python3
"""업체 색인: 업체 검색용 → data/corps.json + data/corpw/{사업자번호 앞 3자리}.json (수집 뒤 워크플로에서 하루 한 번)

출처: 나라장터 공사·물품 낙찰(data/scsbid·data/thng 의 win·winBiz = 최종 낙찰자) + 개찰 상세(data/opening 전체 투찰, 대표자)
      + 국방(data/d2b 상위 30곳·낙찰자, 대표자) + 낙찰 업체 대표자·주소·전화(data/corp_info.json, 수집기 CorpInfo).
corps.json {"v":1, "updated_at", "sidos":[시도…], "items":[[사업자번호, 업체명, 전국 낙찰 수(공사+물품+국방), 마지막 낙찰일,
            낙찰 시도 번호들, 상세 투찰 수, 상세 1순위 수, 국방 투찰 수, 국방 1순위 수, 대표자, 주소, 전화], …]}
corpw/{앞 3자리}.json {사업자번호: [[개찰일, 공고명(40자), 낙찰금액, 발주기관(20자), 업무(공사|물품|국방), 참가수, 시도], …최근 20건]} — 강원 관련 업체만
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
WIN_KEEP = 20
HOME = "강원"   # 낙찰 이력 파일은 이 시·도와 관련된 업체만(강원 낙찰·강원 개찰 상세 투찰·국방 투찰) — 전국 5만 곳은 21MB 라 매일 바꾸기 무거움


def load(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def dumps(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def write_if_changed(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        if p.read_text(encoding="utf-8") == text:
            return
    except FileNotFoundError:
        pass
    p.write_text(text, encoding="utf-8")


def main():
    # 3MB 파일이라 커밋마다 저장소가 커진다 → 하루 한 번만 (FORCE=1 이면 바로)
    old_at = load(DATA / "corps.json").get("updated_at")
    if old_at and not os.environ.get("FORCE"):
        age = dt.datetime.now(KST) - dt.datetime.fromisoformat(old_at)
        if age < dt.timedelta(hours=20):
            print(f"업체 색인: {age.seconds // 3600}시간 전에 만듦 — 건너뜀")
            return
    name, ceo = {}, {}
    wins = collections.Counter()
    last = {}
    wsido = collections.defaultdict(collections.Counter)
    wlist = collections.defaultdict(list)
    sidos = []
    for kind, pat in (("공사", "scsbid"), ("물품", "thng")):
        for f in sorted(glob.glob(str(DATA / pat / "*.json"))):
            for r in load(f).get("items", []):
                b = r.get("winBiz")
                if not b:
                    continue
                wins[b] += 1
                name.setdefault(b, r.get("win") or "")
                d = r.get("date") or ""
                if d > last.get(b, ""):
                    last[b] = d
                    name[b] = r.get("win") or name[b]
                s = r.get("sido") or ""
                if s:
                    if s not in sidos:
                        sidos.append(s)
                    wsido[b][sidos.index(s)] += 1
                wlist[b].append([d, (r.get("nm") or "")[:40], r.get("amt"), (r.get("org") or r.get("dmd") or "")[:20], kind, r.get("cnt"), s])
    dbid, dtop = collections.Counter(), collections.Counter()
    for f in glob.glob(str(DATA / "opening" / "*" / "[0-9]*.json")):
        d = load(f)
        corps = d.get("corps") or []
        for b in (d.get("bids") or {}).values():
            for row in b.get("r") or []:
                c = corps[row[1]]
                biz = c[1]
                if not biz:
                    continue
                dbid[biz] += 1
                if row[0] == 1:
                    dtop[biz] += 1
                name.setdefault(biz, c[0])
                if len(c) > 2 and c[2]:
                    ceo[biz] = c[2]
    mbid, mtop = collections.Counter(), collections.Counter()
    for f in glob.glob(str(DATA / "d2b" / "[0-9]*.json")):
        d = load(f)
        corps = d.get("corps") or []
        for it in d.get("items") or []:
            for row in it.get("r") or []:
                c = corps[row[1]]
                biz = c[1]
                if not biz:
                    continue
                mbid[biz] += 1
                if row[0] == 1:
                    mtop[biz] += 1
                name.setdefault(biz, c[0])
                if len(c) > 2 and c[2]:
                    ceo[biz] = c[2]
            b = it.get("winBiz")
            if b:   # 국방 최종 낙찰
                wins[b] += 1
                name.setdefault(b, it.get("win") or "")
                last[b] = max(last.get(b, ""), it.get("date") or "")
                wlist[b].append([it.get("date") or "", (it.get("nm") or "")[:40], it.get("amt"), (it.get("org") or "")[:20], "국방", it.get("cnt"), ""])
    info = load(DATA / "corp_info.json")   # {biz: [대표자, 주소, 전화]}
    items = []
    for b, nm in name.items():
        ci = info.get(b) or ["", "", ""]
        items.append([b, nm, wins[b], last.get(b, ""), [k for k, _ in wsido[b].most_common(3)],
                      dbid[b], dtop[b], mbid[b], mtop[b], ci[0] or ceo.get(b, ""), ci[1], ci[2]])
    items.sort(key=lambda x: (-(x[2] + x[5]), x[1]))
    body = {"v": 1, "updated_at": dt.datetime.now(KST).isoformat(timespec="seconds"), "sidos": sidos, "items": items}
    write_if_changed(DATA / "corps.json", dumps(body))
    # 최종 낙찰 이력 (앞 3자리별 파일 — 앱이 필요한 것만 불러옴)
    groups = collections.defaultdict(dict)
    home_i = sidos.index(HOME) if HOME in sidos else -1
    for b, lst in wlist.items():
        if not (wsido[b][home_i] or dbid[b] or mbid[b] or any(x[4] == "국방" for x in lst)):
            continue
        lst.sort(key=lambda x: x[0], reverse=True)
        groups[b[:3]][b] = lst[:WIN_KEEP]
    for pre, g in groups.items():
        write_if_changed(DATA / "corpw" / f"{pre}.json", dumps(g))
    print(f"업체 색인 {len(items)}곳 · 낙찰 이력 {len(wlist)}곳 ({len(groups)}개 파일) · 주소 {sum(1 for x in items if x[10])}곳")


if __name__ == "__main__":
    main()

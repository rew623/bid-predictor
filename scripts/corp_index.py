#!/usr/bin/env python3
"""업체 색인: 업체 검색용 → data/corps.json + data/corpw/{사업자번호 앞 3자리}.json (수집 뒤 워크플로에서 하루 한 번)

출처: 나라장터 공사·물품 낙찰(data/scsbid·data/thng 의 win·winBiz = 최종 낙찰자) + 개찰 상세(data/opening 전체 투찰, 대표자)
      + 국방(data/d2b/{연도}/{월}.json 참가 업체 전원·낙찰자, 대표자) + 낙찰 업체 대표자·주소·전화(data/corp_info.json, 수집기 CorpInfo).
corps.json {"v":1, "updated_at", "sidos":[시도…], "items":[[사업자번호, 업체명, 전국 낙찰 수(공사+물품+국방), 마지막 낙찰일,
            낙찰 시도 번호들, 상세 투찰 수, 상세 1순위 수, 국방 투찰 수, 국방 1순위 수, 대표자, 주소, 전화,
            소재지 번호(homes ["시도|시군"…], 주소 → 없으면 시·군 제한 공고 투찰로 추정), 투찰 면허 번호들(lics, 개찰 상세 공고 면허로 추정), 강원 최종 낙찰 수], …], "lics":[면허…]}
corpw/{앞 3자리}.json {사업자번호: [[개찰일, 공고명(40자), 낙찰금액, 발주기관(20자), 업무(공사|물품|국방), 참가수, 시도, 공고ID, 낙찰률, 사정율, 기초금액], …최근 20건]} — 강원 관련 업체만
업체별 투찰 내역(금액·순위)은 앱이 개찰 상세 파일을 직접 읽어 계산한다.
"""
import collections
import datetime as dt
import glob
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT / "scripts"))
import korea  # noqa: E402

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
                wlist[b].append([d, (r.get("nm") or "")[:40], r.get("amt"), (r.get("org") or r.get("dmd") or "")[:20], kind, r.get("cnt"), s,
                                 r.get("id"), r.get("rate"), r.get("sr"), r.get("base")])
    dbid, dtop = collections.Counter(), collections.Counter()
    blic = collections.defaultdict(collections.Counter)   # 업체가 투찰한 공고의 면허 (업체 면허 추정 — 면허 자료가 따로 없음)
    bsgg = collections.defaultdict(collections.Counter)   # 시·군 하나로 참가 제한한 공고에 투찰한 시·군 (소재지 추정 — 낙찰 못 한 업체는 주소가 없음)
    recs = {}
    for f in glob.glob(str(DATA / "scsbid" / "*.json")):
        for r in load(f).get("items", []):
            recs[r["id"]] = (r.get("lic") or [], r.get("rgn"))
    for f in glob.glob(str(DATA / "opening" / "*" / "[0-9]*.json")):
        d = load(f)
        corps = d.get("corps") or []
        for bid, b in (d.get("bids") or {}).items():
            lic, rgn = recs.get(bid, ([], None))
            one = None
            if rgn and len(rgn) == 1 and len(rgn[0].split()) >= 2:
                sd, sg = korea.parse_region(rgn[0])
                one = f"{sd}|{sg}" if sd and sg else None
            for ci in b.get("c") or [row[1] for row in b.get("r") or []]:
                biz = corps[ci][1]
                if biz:
                    for l in lic:
                        blic[biz][l] += 1
                    if one:
                        bsgg[biz][one] += 1
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
    corps = load(DATA / "d2b" / "corps.json").get("corps") or []   # 모든 월이 같이 쓰는 업체 표
    for f in glob.glob(str(DATA / "d2b" / "[0-9]*" / "[0-9]*.json")):   # v2 월 파일: c = 참가 업체 전원(순위 순, 앞 k 곳이 순위 있음), 없으면(12개월 전) r 상위 30곳
        d = load(f)
        for it in d.get("items") or []:
            for pos, ci in enumerate(it.get("c") or [row[1] for row in it.get("r") or []]):
                c = corps[ci]
                biz = c[1]
                if not biz:
                    continue
                mbid[biz] += 1
                if pos == 0 and it.get("k"):
                    mtop[biz] += 1
                name.setdefault(biz, c[0])
                if len(c) > 2 and c[2]:
                    ceo[biz] = c[2]
            b = it.get("winBiz")
            if b:   # 국방 최종 낙찰
                wins[b] += 1
                name.setdefault(b, it.get("win") or "")
                last[b] = max(last.get(b, ""), it.get("date") or "")
                wlist[b].append([it.get("date") or "", (it.get("nm") or "")[:40], it.get("amt"), (it.get("org") or "")[:20], "국방", it.get("cnt"), "",
                                 it.get("id"), it.get("rate"), it.get("sr"), it.get("base")])
    info = load(DATA / "corp_info.json")   # {biz: [대표자, 주소, 전화]}
    home_i = sidos.index(HOME) if HOME in sidos else -1
    lics = sorted({l for c in blic.values() for l in c})
    items, homes, hidx = [], [], {}
    for b, nm in name.items():
        ci = info.get(b) or ["", "", ""]
        sd, sg = korea.parse_region(ci[1]) if ci[1] else (None, None)
        home = f"{sd}|{sg or ''}" if sd else (bsgg[b].most_common(1)[0][0] if bsgg[b] else "")
        # 면허: 그 면허 공고에 3번↑ 그리고 투찰의 10%↑ (여러 면허 공고라 한두 번은 우연히 섞임)
        tot = dbid[b] or 1
        lx = [lics.index(l) for l, n in blic[b].most_common(6) if n >= 3 and n >= tot * 0.1]
        if home not in hidx:
            hidx[home] = len(homes)
            homes.append(home)
        items.append([b, nm, wins[b], last.get(b, ""), [k for k, _ in wsido[b].most_common(3)],
                      dbid[b], dtop[b], mbid[b], mtop[b], ci[0] or ceo.get(b, ""), ci[1], ci[2],
                      hidx[home], lx, wsido[b][home_i] if home_i >= 0 else 0])
    items.sort(key=lambda x: (-(x[2] + x[5]), x[1]))
    body = {"v": 1, "updated_at": dt.datetime.now(KST).isoformat(timespec="seconds"), "sidos": sidos, "lics": lics, "homes": homes, "items": items}
    write_if_changed(DATA / "corps.json", dumps(body))
    # 최종 낙찰 이력 (앞 3자리별 파일 — 앱이 필요한 것만 불러옴)
    groups = collections.defaultdict(dict)
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

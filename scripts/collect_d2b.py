#!/usr/bin/env python3
"""국방전자조달(D2B, 방위사업청) 입찰결과 수집기 — 물품·용역(국내 경쟁) + 시설공사(경쟁).

나라장터와 따로 도는 수집기다(호출 한도·서비스가 다름). GitHub Actions 에서 collect.py 뒤에 실행.
openapi.d2b.go.kr 는 서비스키 없이 응답한다(2026-09-26 확인, User-Agent 필요). 안 되면
공공데이터포털 게이트웨이(apis.data.go.kr/1690000, DATA_GO_KR_KEY, 개발계정 하루 100회)로 바꿔 시도.

공고 1건 = 상세(기초예비가격·사정률 범위·낙찰하한율) + 참가업체 전원(투찰금액·순위) + 복수예가 15개(추첨 여부).
예정가격 = 추첨된 복수예가(보통 4개) 평균 — 1순위 투찰률로 역산한 값과 0.001% 안에서 일치(2026-09 확인).

환경변수
  MAX_MINUTES     이 시간이 지나면 저장하고 종료 (기본 25)
  D2B_MONTHS      과거로 거슬러 받을 개월 수 (기본 24)
  DATA_GO_KR_KEY  게이트웨이로 바꿀 때만 사용

결과
  data/d2b/{연도}.json   {"v":1, "year", "corps":[[업체명, 사업자번호]], "items":[…]} (아래 항목)
  data/d2b/meta.json     {"updated_at", "files":{연도:[경로]}, "counts":{연도:건수}, "backfill":{cursor, oldest, done}}
  data/d2b/index.json    수집기 전용 {"done":{id:1}, "fail":{id:횟수}}
항목: id, kind(물품|용역|시설), nm, org, cm(계약방법), dm(낙찰자결정방법), bm(총액제·단가제), date(YYYY-MM-DD),
      base(기초예비가격), budget(예산), rng([하한%, 상한%]), floor(낙찰하한율, 없으면 생략), plan(예정가격), sr(=plan/base×100),
      cnt(투찰 업체 수), amt·rate(1순위 금액·투찰률), win·winBiz, p([[번호, 예비가격, 추첨 0/1]]), r(상위 30곳 [[순위(0=순위 없음), corps 인덱스, 금액, 비고?]]),
      h(전원 분포 [[round(금액/기초×1000), 곳수]])
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(os.environ.get("D2B_OUT") or ROOT / "data" / "d2b")   # D2B_OUT 은 로컬 시험용
KST = dt.timezone(dt.timedelta(hours=9))
MAX_MINUTES = float(os.environ.get("MAX_MINUTES") or 25)
MONTHS = int(os.environ.get("D2B_MONTHS") or 24)
RECENT_DAYS = 30
TOP = 30             # 개찰 순위를 행으로 남길 상위 업체 수
FAIL_STOP = 5          # 연속 실패가 이만큼이면 이번 실행은 멈춘다
MAX_TRIES = 3          # 공고 하나를 이만큼 실패하면 건너뛴다
BASES = ["http://openapi.d2b.go.kr/openapi/service/BidResultInfoService/",
         "https://apis.data.go.kr/1690000/BidResultInfoService/"]
KINDS = {
    "D": dict(list="getDmstcCmpetBidResultList", detail="getDmstcCmpetBidResultDetail",
              mnuf="getDmstcCmpetBidResultMnufList", bsic="getDmstcCmpetBidResultBsicList"),
    "F": dict(list="getFcltyCmpetBidResultList", detail="getFcltyCmpetBidResultDetail",
              mnuf="getFcltyCmpetBidResultMnufList", bsic="getFcltyCmpetBidResultBsicList"),
}
DONE_RESULTS = ("낙찰", "순위확정")


def now_kst():
    return dt.datetime.now(KST)


def log(*a):
    print(now_kst().strftime("%H:%M:%S"), *a, flush=True)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def write_if_changed(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except FileNotFoundError:
        pass
    path.write_text(text, encoding="utf-8")
    return True


def num(v):
    try:
        x = float(str(v).replace(",", "").strip())
        return x
    except (TypeError, ValueError):
        return None


class Stop(Exception):
    pass


class Api:
    def __init__(self, deadline):
        self.deadline = deadline
        self.key = (os.environ.get("DATA_GO_KR_KEY") or "").strip()
        self.base = 0
        self.fails = 0
        self.calls = 0

    def call(self, op, **params):
        """items 목록과 totalCount 를 돌려준다. 오류면 예외"""
        if time.time() > self.deadline:
            raise Stop("시간 끝")
        p = {k: v for k, v in params.items() if v not in (None, "")}
        if self.base == 1:
            p["serviceKey"] = self.key
        url = BASES[self.base] + op + "?" + urllib.parse.urlencode(p)
        last = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (bid-predictor collector)"})
                with urllib.request.urlopen(req, timeout=90) as r:
                    t = r.read().decode("utf-8", "replace")
                self.calls += 1
                code = re.search(r"<resultCode>([^<]*)", t)
                if not code:
                    raise RuntimeError("응답 형식 이상: " + re.sub(r"\s+", " ", t[:120]))
                if code.group(1) not in ("00", "0"):
                    msg = re.search(r"<resultMsg>([^<]*)", t)
                    raise RuntimeError(f"{code.group(1)} {msg.group(1) if msg else ''}")
                items = [dict(re.findall(r"<(\w+)>([^<]*)</\1>", it)) for it in re.findall(r"<item>(.*?)</item>", t, re.S)]
                tc = re.search(r"<totalCount>(\d+)", t)
                self.fails = 0
                return items, int(tc.group(1)) if tc else len(items)
            except (urllib.error.URLError, TimeoutError, ConnectionError, RuntimeError) as e:
                last = e
                time.sleep(5 * (attempt + 1))
        self.fails += 1
        if self.fails >= FAIL_STOP:
            if self.base == 0 and self.key:
                log("D2B 직접 주소가 안 됨 → 공공데이터포털 게이트웨이로 바꿈")
                self.base, self.fails = 1, 0
            else:
                raise Stop(f"연속 {self.fails}번 실패: {last}")
        raise RuntimeError(str(last))

    def list_all(self, kind, bgn, end):
        """개찰일 bgn~end(YYYYMMDD)의 결과 목록 전체"""
        out, page = [], 1
        while True:
            items, total = self.call(KINDS[kind]["list"], numOfRows=100, pageNo=page, opengDateBegin=bgn, opengDateEnd=end)
            out += items
            if not items or len(out) >= total:
                return out
            page += 1


def rid(kind, it):
    if kind == "D":
        return "D-" + "-".join(it.get(k, "").strip() for k in ("demandYear", "orntCode", "dcsNo", "iemNo", "pblancNo", "pblancOdr"))
    return "F-" + "-".join(it.get(k, "").strip() for k in ("orntCode", "cntrwkNo", "pblancNo", "pblancOdr"))


class Store:
    """연도별 파일. 업체 표(corps)는 파일마다"""
    def __init__(self):
        self.years = {}
        self.dirty = set()

    def year(self, y):
        if y not in self.years:
            d = load_json(OUT / f"{y}.json", None) or {"v": 1, "year": y, "corps": [], "items": []}
            d["_ci"] = {tuple(c): i for i, c in enumerate(d["corps"])}
            d["_ids"] = {it["id"]: i for i, it in enumerate(d["items"])}
            self.years[y] = d
        return self.years[y]

    def corp(self, d, name, biz):
        k = (name, biz)
        if k not in d["_ci"]:
            d["_ci"][k] = len(d["corps"])
            d["corps"].append([name, biz])
        return d["_ci"][k]

    def add(self, rec, bidders):
        d = self.year(rec["date"][:4])
        # 참가 업체가 공고당 평균 500곳(최대 1만)이라 전원을 저장하면 1년에 200MB를 넘는다 → 상위 TOP 곳만 행으로,
        # 전체는 기초금액 대비 투찰률 분포 h = [[round(금액/기초×1000), 곳수]] (0.1% 간격)로 남긴다
        rows = [[rk, self.corp(d, nm, biz), amt] + ([note] if note else []) for rk, nm, biz, amt, rate, note in bidders[:TOP]]
        rec["r"] = rows
        if rec.get("base"):
            h = {}
            for b in bidders:
                k = round(b[3] / rec["base"] * 1000)
                h[k] = h.get(k, 0) + 1
            rec["h"] = sorted([k, n] for k, n in h.items())
        if rec["id"] in d["_ids"]:
            d["items"][d["_ids"][rec["id"]]] = rec
        else:
            d["_ids"][rec["id"]] = len(d["items"])
            d["items"].append(rec)
        self.dirty.add(d["year"])

    def save(self):
        files, counts = {}, {}
        for p in sorted(OUT.glob("[0-9][0-9][0-9][0-9].json")):
            y = p.stem
            if y in self.years:
                d = self.years[y]
                d["items"].sort(key=lambda it: (it["date"], it["id"]), reverse=True)
                d["_ids"] = {it["id"]: i for i, it in enumerate(d["items"])}
            files[y] = [f"d2b/{y}.json"]
        for y in self.dirty:
            d = self.years[y]
            d["items"].sort(key=lambda it: (it["date"], it["id"]), reverse=True)
            d["_ids"] = {it["id"]: i for i, it in enumerate(d["items"])}
            write_if_changed(OUT / f"{y}.json", dumps({k: v for k, v in d.items() if not k.startswith("_")}))
            files[y] = [f"d2b/{y}.json"]
        self.dirty.clear()
        for y in files:
            counts[y] = len(self.year(y)["items"])
        return files, counts


def fetch_one(api, kind, it):
    """결과 1건 → (레코드, 참가업체 목록) 또는 None(결과 없음)"""
    K = KINDS[kind]
    if kind == "D":
        key = dict(demandYear=it.get("demandYear"), orntCode=it.get("orntCode"), dcsNo=it.get("dcsNo"),
                   iemNo=it.get("iemNo"), opengDate=it.get("opengDate"))
        det, _ = api.call(K["detail"], numOfRows=1, pblancNo=it.get("pblancNo"), pblancOdr=it.get("pblancOdr"), **key)
        mn, _ = api.call(K["mnuf"], numOfRows=9999, **key)
        bs, _ = api.call(K["bsic"], numOfRows=99, **key)
        prices = [(int(num(x.get("bsicSeqn")) or 0), num(x.get("prdfPrce")), x.get("choiYsno") == "Y") for x in bs]
    else:
        key = dict(orntCode=it.get("orntCode"), cntrwkNo=it.get("cntrwkNo"))
        det, _ = api.call(K["detail"], numOfRows=1, opengDate=it.get("opengDate"), pblancNo=it.get("pblancNo"), pblancOdr=it.get("pblancOdr"), **key)
        mn, _ = api.call(K["mnuf"], numOfRows=9999, ntatPlanDate=it.get("opengDate"), **key)
        bs, _ = api.call(K["bsic"], numOfRows=99, ntatPlanDate=it.get("opengDate"), **key)
        prices = [(int(num(x.get("epprRank")) or 0), num(x.get("planPrce")), x.get("choiYsno") == "Y") for x in bs]
    d = det[0] if det else {}
    base = num(d.get("bsisPreparPc"))
    chosen = [p for _, p, c in prices if c and p]
    plan = round(sum(chosen) / len(chosen)) if chosen else None
    bidders = []
    for x in mn:
        amt = num(x.get("tbidAmnt")) or num(x.get("tbidUtpr"))
        rate = num(x.get("bidnRate"))
        rk = int(num(x.get("bidnRank")) or 0)
        note = (x.get("bidxNote") or "").strip()
        if not amt:
            continue
        bidders.append((rk, (x.get("mfkrName") or "").strip(), re.sub(r"\D", "", x.get("bznsRgnb") or ""), int(amt), round(rate, 4) if rate else None, note))
    bidders.sort(key=lambda b: (b[0] == 0, b[0], b[3]))
    date = (it.get("opengDate") or d.get("opengDt", "")[:8])
    lo, hi = num(d.get("asessRtLwlt")), num(d.get("asessRtUplmt"))
    floor = num(d.get("scsbidLwltRt"))
    top = next((b for b in bidders if b[0] == 1), None) or next((b for b in bidders if b[5] == "낙찰"), None)
    rec = {
        "id": rid(kind, it), "kind": "시설" if kind == "F" else "용역" if "용역" in (d.get("excutTy") or it.get("excutTy") or "") else "물품",
        "nm": (it.get("bidNm") or it.get("cntrwkNm") or d.get("bidNm") or d.get("cntrwkNm") or "").strip(),
        "org": (it.get("ornt") or d.get("ornt") or "").strip(), "cm": it.get("cntrctMth") or d.get("cntrctMth"),
        "dm": it.get("sucbidrDecsnMth") or d.get("sucbidrDecsnMth"), "bm": it.get("bidMth") or d.get("bidMth"),
        "date": f"{date[:4]}-{date[4:6]}-{date[6:8]}", "base": int(base) if base else None,
        "budget": int(num(d.get("budgetAmount"))) if num(d.get("budgetAmount")) else None,
        "rng": [lo, hi] if (lo or hi) else None, "floor": floor if floor else None,
        "plan": plan, "sr": round(plan / base * 100, 4) if plan and base else None, "cnt": len(bidders),
        "amt": top[3] if top else None, "rate": top[4] if top else None,
        "win": (d.get("scsbidEntrpsNm") or (top[1] if top else "")).strip() or None,
        "winBiz": re.sub(r"\D", "", d.get("scsbidEntrpsBsnmRegistNo") or (top[2] if top else "")) or None,
        "p": [[s, int(p), 1 if c else 0] for s, p, c in sorted(prices) if p],
    }
    if rec["sr"] is not None and not (80 <= rec["sr"] <= 120):
        rec["sr"] = None
    return {k: v for k, v in rec.items() if v not in (None, "", [], {})}, bidders


def process(api, store, index, kind, items, label):
    todo = [it for it in items if it.get("bidResult") in DONE_RESULTS and rid(kind, it) not in index["done"]
            and index["fail"].get(rid(kind, it), 0) < MAX_TRIES]
    log(f"{label} {kind}: 목록 {len(items)} · 새로 받을 {len(todo)}")
    n = 0
    for it in todo:
        i = rid(kind, it)
        try:
            rec, bidders = fetch_one(api, kind, it)
        except Stop:
            raise
        except Exception as e:  # noqa: BLE001 — 한 건 실패는 기록하고 다음으로
            index["fail"][i] = index["fail"].get(i, 0) + 1
            log("  실패", i, e)
            continue
        if bidders:
            store.add(rec, bidders)
        index["done"][i] = 1
        index["fail"].pop(i, None)
        n += 1
        if n % 50 == 0:
            save(store, index, None)
    return n


def save(store, index, meta):
    files, counts = store.save()
    write_if_changed(OUT / "index.json", dumps(index))
    if meta is not None:
        m = load_json(OUT / "meta.json", {})
        m.update(meta)
        old_files = m.get("files") or {}
        old_files.update(files)
        m["files"] = dict(sorted(old_files.items(), reverse=True))
        m["counts"] = {**(m.get("counts") or {}), **counts}
        m["total"] = sum(m["counts"].values())
        write_if_changed(OUT / "meta.json", dumps(m))


def main():
    t0 = time.time()
    api = Api(t0 + MAX_MINUTES * 60)
    store = Store()
    index = load_json(OUT / "index.json", {"done": {}, "fail": {}})
    meta = load_json(OUT / "meta.json", {})
    if not index.get("done"):   # 진행 기록이 없거나 지워졌으면 저장된 결과에서 다시 만든다
        for p in OUT.glob("[0-9][0-9][0-9][0-9].json"):
            for it in load_json(p, {}).get("items", []):
                index["done"][it["id"]] = 1
        index.setdefault("fail", {})
        if index["done"]:
            log(f"진행 기록 복구: 저장된 결과 {len(index['done'])}건")
    now = now_kst()
    bf = meta.get("backfill") or {}
    changed = 0
    try:
        # ① 최근 30일 (매번)
        bgn, end = (now - dt.timedelta(days=RECENT_DAYS)).strftime("%Y%m%d"), now.strftime("%Y%m%d")
        for kind in KINDS:
            changed += process(api, store, index, kind, api.list_all(kind, bgn, end), "최근")
        # ② 과거: 한 달씩 거슬러 (최근 30일 이전부터 MONTHS 개월까지)
        oldest = (now.replace(day=1) - dt.timedelta(days=31 * MONTHS)).strftime("%Y%m01")
        cursor = bf.get("cursor") or (now - dt.timedelta(days=RECENT_DAYS + 1)).strftime("%Y%m%d")
        while not bf.get("done"):
            c = dt.datetime.strptime(cursor, "%Y%m%d")
            start = c.replace(day=1)
            for kind in KINDS:
                changed += process(api, store, index, kind, api.list_all(kind, start.strftime("%Y%m%d"), cursor), "과거 " + start.strftime("%Y-%m"))
            cursor = (start - dt.timedelta(days=1)).strftime("%Y%m%d")
            bf = {"cursor": cursor, "oldest": start.strftime("%Y%m%d"), "done": cursor < oldest}
            save(store, index, {"backfill": bf})
    except Stop as e:
        log("멈춤:", e)
    except RuntimeError as e:
        log("접속 실패로 멈춤:", e)
    finally:
        if api.calls or store.dirty:   # 한 번도 못 받았으면 기록을 건드리지 않는다
            save(store, index, {"backfill": bf, "updated_at": now.isoformat(timespec="seconds"), "calls": api.calls,
                                "base": BASES[api.base].split("/")[2]})
        log(f"D2B 끝: 새 결과 {changed}건 · 호출 {api.calls}회 · {(time.time() - t0) / 60:.1f}분")


if __name__ == "__main__":
    main()

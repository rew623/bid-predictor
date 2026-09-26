#!/usr/bin/env python3
"""국방전자조달(D2B, 방위사업청) 입찰결과 수집기 — 경쟁입찰(물품·용역, 시설) + 공개수의(물품·용역, 시설), 참가 업체 전원.

나라장터와 따로 도는 수집기다(호출 한도·서비스가 다름). GitHub Actions 에서 collect.py 와 동시에 실행.
openapi.d2b.go.kr 는 서비스키 없이 응답한다(2026-09-26 확인, User-Agent 필요). 안 되면
공공데이터포털 게이트웨이(apis.data.go.kr/1690000, DATA_GO_KR_KEY)로 바꿔 시도.

공고 1건 = 상세(기초예비가격·사정률 범위·낙찰하한율) + 참가업체 전원(투찰금액·순위) + 복수예가 15개(추첨 여부).
예정가격 = 추첨된 복수예가(보통 4개) 평균 — 1순위 투찰률로 역산한 값과 0.001% 안에서 일치(2026-09 확인).

환경변수
  MAX_MINUTES     이 시간이 지나면 저장하고 종료 (기본 25)
  D2B_MONTHS      과거로 거슬러 받을 개월 수 (기본 24)
  DATA_GO_KR_KEY  게이트웨이로 바꿀 때만 사용

결과 (v2, 2026-09-26 — 참가 업체 전원)
  data/d2b/{연도}/{월}.json  {"v":2, "month":"YYYY-MM", "items":[…]} (개찰월별)
  data/d2b/corps.json       {"v":2, "corps":[[업체명, 사업자번호, 대표자?]]} — 모든 월 파일이 같이 쓰는 업체 표(월마다 두면 한 달 1.4MB씩 반복)
  data/d2b/meta.json        {"updated_at", "files":{"YYYY-MM":[경로]}, "counts":{"YYYY-MM":건수}, "total", "backfill":{cursor, oldest, done}}
  data/d2b/index.json       수집기 전용 {"v":2, "done":{id:1}, "fail":{id:횟수}}
항목: id(D-·F- 경쟁, N-·NF- 공개수의), kind(물품|용역|시설), cm(계약방법), dm, bm, nm, org, date, base(기초예비가격), budget, rng, floor,
      plan(예정가격), sr, cnt(투찰 업체 수), amt·rate(1순위), win·winBiz(최종 낙찰), p([[번호, 예비가격, 추첨 0/1]]),
      r(상위 30곳 [[순위(0=순위 없음), corps 인덱스, 금액, 비고?]]),
      c·x(참가 업체 전원, 순위 순 — c = corps 인덱스, x = 투찰률 ×1000 정수를 앞 값과의 차이로(첫 값은 그대로), 순위 있는 업체가 앞 k 곳)
      — 최근 FULL_MONTHS(12)개월만. 그 전 달은 r(상위 30곳)·요약만. 금액은 투찰률 × 예정가격으로 거의 복원
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from korea import normalize_licenses  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(os.environ.get("D2B_OUT") or ROOT / "data" / "d2b")   # D2B_OUT 은 로컬 시험용
KST = dt.timezone(dt.timedelta(hours=9))
MAX_MINUTES = float(os.environ.get("MAX_MINUTES") or 25)
MONTHS = int(os.environ.get("D2B_MONTHS") or 24)
RECENT_DAYS = 30
TOP = 30             # 금액·비고까지 행으로 남길 상위 업체 수 (전원은 c·x 에 업체·투찰률로)
FULL_MONTHS = 12     # 참가 업체 전원(c·x)을 남길 최근 개월 수 — 24개월 전원이면 약 570MB
# 상위 30위 밖이어도 금액 행을 남길 사업자번호(쉼표 구분) — 저장소 변수 WATCH_BIZ(공개 코드에 안 넣음). 우리 업체 국방 투찰 금액용
WATCH = {re.sub(r"\D", "", b) for b in (os.environ.get("WATCH_BIZ") or "").split(",") if re.sub(r"\D", "", b)}
FAIL_STOP = 5          # 연속 실패가 이만큼이면 이번 실행은 멈춘다
MAX_TRIES = 3          # 공고 하나를 이만큼 실패하면 건너뛴다
BASES = ["http://openapi.d2b.go.kr/openapi/service/",
         "https://apis.data.go.kr/1690000/"]   # 뒤에 서비스 이름(BidResultInfoService·BidPblancInfoService)/오퍼레이션
# 목록 날짜 조건: 경쟁 = 개찰일(opengDate), 공개수의 = 협상완료일(ntatComptDate)
KINDS = {
    "D": dict(list="getDmstcCmpetBidResultList", detail="getDmstcCmpetBidResultDetail",
              mnuf="getDmstcCmpetBidResultMnufList", bsic="getDmstcCmpetBidResultBsicList", dt="opengDate"),
    "F": dict(list="getFcltyCmpetBidResultList", detail="getFcltyCmpetBidResultDetail",
              mnuf="getFcltyCmpetBidResultMnufList", bsic="getFcltyCmpetBidResultBsicList", dt="opengDate"),
    "N": dict(list="getDmstcOthbcVltrnNtatResultList", detail="getDmstcOthbcVltrnNtatResultDetail",
              mnuf="getDmstcOthbcVltrnNtatResultMnufList", bsic="getDmstcOthbcVltrnNtatResultBsicList", dt="ntatComptDate"),
    "NF": dict(list="getFcltyOthbcVltrnNtatResultList", detail="getFcltyOthbcVltrnNtatResultDetail",
               mnuf="getFcltyOthbcVltrnNtatResultMnufList", bsic="getFcltyOthbcVltrnNtatResultBsicList", dt="ntatComptDate"),
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

    def call(self, op, svc="BidResultInfoService", **params):
        """items 목록과 totalCount 를 돌려준다. 오류면 예외"""
        if time.time() > self.deadline:
            raise Stop("시간 끝")
        p = {k: v for k, v in params.items() if v not in (None, "")}
        if self.base == 1:
            p["serviceKey"] = self.key
        url = BASES[self.base] + svc + "/" + op + "?" + urllib.parse.urlencode(p)
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
            f = KINDS[kind]["dt"]
            items, total = self.call(KINDS[kind]["list"], numOfRows=100, pageNo=page, **{f + "Begin": bgn, f + "End": end})
            out += items
            if not items or len(out) >= total:
                return out
            page += 1


def rid(kind, it):
    if kind in ("D", "N"):
        return kind + "-" + "-".join((it.get(k) or "").strip() for k in ("demandYear", "orntCode", "dcsNo", "iemNo", "pblancNo", "pblancOdr"))
    return kind + "-" + "-".join((it.get(k) or "").strip() for k in ("orntCode", "cntrwkNo", "pblancNo", "pblancOdr"))


def result_of(it):
    return (it.get("bidResult") or it.get("ntatResult") or "").strip()


class Store:
    """개찰월별 파일 data/d2b/{연도}/{월}.json + 모든 달이 같이 쓰는 업체 표 data/d2b/corps.json"""
    def __init__(self):
        self.months = {}
        self.dirty = set()
        self.saved_files, self.saved_counts = {}, {}
        self.corps = (load_json(OUT / "corps.json", {}) or {}).get("corps") or []
        self.ci = {(c[0], c[1]): i for i, c in enumerate(self.corps)}
        self.corps_dirty = False
        self.full_from = (now_kst().replace(day=1) - dt.timedelta(days=31 * (FULL_MONTHS - 1))).strftime("%Y-%m")

    @staticmethod
    def path(m):
        return OUT / m[:4] / f"{m[5:7]}.json"

    def month(self, m):
        if m not in self.months:
            d = load_json(self.path(m), None) or {"v": 2, "month": m, "items": []}
            d["_ids"] = {it["id"]: i for i, it in enumerate(d["items"])}
            self.months[m] = d
        return self.months[m]

    def corp(self, name, biz, ceo=None):
        k = (name, biz)
        if k not in self.ci:
            self.ci[k] = len(self.corps)
            self.corps.append([name, biz] + ([ceo] if ceo else []))
            self.corps_dirty = True
        elif ceo and len(self.corps[self.ci[k]]) < 3:
            self.corps[self.ci[k]].append(ceo)
            self.corps_dirty = True
        return self.ci[k]

    def add(self, rec, bidders):
        m = rec["date"][:7]
        d = self.month(m)
        rec["r"] = [[rk, self.corp(nm, biz, ceo), amt] + ([note] if note else []) for rk, nm, biz, amt, rate, note, ceo in bidders[:TOP] + [b for b in bidders[TOP:] if b[2] in WATCH]]
        rec["k"] = sum(1 for b in bidders if b[0] > 0)
        if m >= self.full_from:
            rec["c"] = [self.corp(nm, biz, ceo) for rk, nm, biz, amt, rate, note, ceo in bidders]
            xs = [round(rate * 1000) if rate else 0 for rk, nm, biz, amt, rate, note, ceo in bidders]
            rec["x"] = xs[:1] + [b - a for a, b in zip(xs, xs[1:])]
        if rec["id"] in d["_ids"]:
            d["items"][d["_ids"][rec["id"]]] = rec
        else:
            d["_ids"][rec["id"]] = len(d["items"])
            d["items"].append(rec)
        self.dirty.add(m)

    def save(self):
        if self.corps_dirty:
            write_if_changed(OUT / "corps.json", dumps({"v": 2, "corps": self.corps}))
            self.corps_dirty = False
        for m in self.dirty:
            d = self.months[m]
            d["items"].sort(key=lambda it: (it["date"], it["id"]), reverse=True)
            d["_ids"] = {it["id"]: i for i, it in enumerate(d["items"])}
            write_if_changed(self.path(m), dumps({k: v for k, v in d.items() if not k.startswith("_")}))
            self.saved_files[m] = [f"d2b/{m[:4]}/{m[5:7]}.json"]
            self.saved_counts[m] = len(d["items"])
        self.dirty.clear()
        return dict(self.saved_files), dict(self.saved_counts)   # 이번 실행에서 저장한 달 전부 (중간 저장 때 meta 를 안 써도 빠지지 않게)


def rank_of(x):
    """참가업체 행의 순위: 경쟁 = bidnRank, 공개수의 = negnNote('1순위')"""
    rk = int(num(x.get("bidnRank")) or 0)
    if not rk:
        m = re.match(r"\s*(\d+)\s*순위", x.get("negnNote") or x.get("bidxNote") or "")
        rk = int(m.group(1)) if m else 0
    return rk


def fetch_one(api, kind, it):
    """결과 1건 → (레코드, 참가업체 목록)"""
    K = KINDS[kind]
    day = it.get("opengDate") or it.get("ntatPlanDate")
    if kind in ("D", "N"):
        key = dict(demandYear=it.get("demandYear"), orntCode=it.get("orntCode"), dcsNo=it.get("dcsNo"), iemNo=it.get("iemNo"), opengDate=day)
        extra = {"ntatPlanDate": day} if kind == "N" else {"opengDate": day}
        det, _ = api.call(K["detail"], numOfRows=1, pblancNo=it.get("pblancNo"), pblancOdr=it.get("pblancOdr"),
                          **{k: v for k, v in key.items() if k != "opengDate"}, **extra)
        mn, _ = api.call(K["mnuf"], numOfRows=9999, **key)
        bs, _ = api.call(K["bsic"], numOfRows=99, **key)
    else:
        key = dict(orntCode=it.get("orntCode"), cntrwkNo=it.get("cntrwkNo"))
        if kind == "F":
            det, _ = api.call(K["detail"], numOfRows=1, opengDate=day, pblancNo=it.get("pblancNo"), pblancOdr=it.get("pblancOdr"), **key)
        else:
            det, _ = api.call(K["detail"], numOfRows=1, ntatPlanDate=day, pblancOdr=it.get("pblancOdr"), **key)
        mn, _ = api.call(K["mnuf"], numOfRows=9999, ntatPlanDate=day, **key)
        bs, _ = api.call(K["bsic"], numOfRows=99, ntatPlanDate=day, **key)
    prices = [(int(num(x.get("bsicSeqn") or x.get("epprRank")) or 0), num(x.get("prdfPrce") or x.get("planPrce")), x.get("choiYsno") == "Y") for x in bs]
    d = det[0] if det else {}
    base = num(d.get("bsisPreparPc"))
    chosen = [p for _, p, c in prices if c and p]
    plan = round(sum(chosen) / len(chosen)) if chosen else None
    bidders = []
    for x in mn:
        amt = num(x.get("tbidAmnt")) or num(x.get("tbidUtpr")) or num(x.get("tnegnAmnt")) or num(x.get("vnegnAmnt")) or num(x.get("vnegnUtpr"))
        rate = num(x.get("bidnRate") or x.get("negnRate"))
        note = (x.get("bidxNote") or x.get("negnNote") or "").strip()
        if not amt:
            continue
        bidders.append((rank_of(x), (x.get("mfkrName") or "").strip(), re.sub(r"\D", "", x.get("bznsRgnb") or ""), int(amt), round(rate, 4) if rate else None,
                        note[:12], (x.get("rptrKore") or "").strip() or None))
    bidders.sort(key=lambda b: (b[0] == 0, b[0], b[3]))
    date = day or re.sub(r"\D", "", d.get("opengDt") or d.get("othbcNtatDt") or "")[:8]
    lo, hi = num(d.get("asessRtLwlt")), num(d.get("asessRtUplmt"))
    floor = num(d.get("scsbidLwltRt"))
    top = next((b for b in bidders if b[0] == 1), None) or next((b for b in bidders if "낙찰" in b[5]), None)
    busi = d.get("excutTy") or it.get("excutTy") or ""
    rec = {
        "id": rid(kind, it), "kind": "시설" if kind in ("F", "NF") else "용역" if "용역" in busi else "물품",
        "nm": (it.get("bidNm") or it.get("cntrwkNm") or it.get("othbcNtatNm") or d.get("bidNm") or d.get("cntrwkNm") or d.get("othbcNtatNm") or "").strip(),
        "org": (it.get("ornt") or d.get("ornt") or "").strip(), "cm": it.get("cntrctMth") or d.get("cntrctMth") or ("수의계약" if kind in ("N", "NF") else None),
        "dm": it.get("sucbidrDecsnMth") or d.get("sucbidrDecsnMth"), "bm": it.get("bidMth") or d.get("bidMth"),
        "date": f"{date[:4]}-{date[4:6]}-{date[6:8]}", "base": int(base) if base else None,
        "budget": int(num(d.get("budgetAmount") or it.get("budgetAmount"))) if num(d.get("budgetAmount") or it.get("budgetAmount")) else None,
        "rng": [lo, hi] if (lo or hi) else None, "floor": floor if floor else None,
        "plan": plan, "sr": round(plan / base * 100, 4) if plan and base else None, "cnt": len(bidders),
        "amt": top[3] if top else None, "rate": top[4] if top else None,
        "win": (d.get("scsbidEntrpsNm") or (top[1] if top else "")).strip() or None,
        "winBiz": re.sub(r"\D", "", d.get("scsbidEntrpsBsnmRegistNo") or (top[2] if top else "")) or None,
        "p": [[sq, int(pr), 1 if c else 0] for sq, pr, c in sorted(prices) if pr],
    }
    if rec["sr"] is not None and not (80 <= rec["sr"] <= 120):
        rec["sr"] = None
    return {k: v for k, v in rec.items() if v not in (None, "", [], {})}, bidders


def process(api, store, index, kind, items, label):
    todo = [it for it in items if result_of(it) in DONE_RESULTS and rid(kind, it) not in index["done"]
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
        m["files"] = dict(sorted(((k, v) for k, v in old_files.items() if len(k) == 7), reverse=True))   # 'YYYY-MM' (v1 연도 키는 버림)
        m["counts"] = {k: v for k, v in {**(m.get("counts") or {}), **counts}.items() if len(k) == 7}
        m["total"] = sum(m["counts"].values())
        write_if_changed(OUT / "meta.json", dumps(m))


# ---------------------------------------------------------------- 진행중 국방 공고 → data/d2b/bids.json
# 물품·용역(국내 경쟁) + 시설(공사). 목록(개찰일 오늘~60일) + 공고마다 상세 1회(지역제한·면허제한·추정가격·사정률·낙찰하한율).
# 항목: id, src='국방', kind(물품|용역|공사), nm, org, cm, dm, bm, reg(입찰참가등록 마감), close(입찰서 제출 마감), open,
#       base(기초예비가격), est, budget, rng[하한%, 상한%], floor, rgn[지역 이름], inds[면허·업종 이름], url
NOTICE_KINDS = {
    "D": dict(list="getDmstcCmpetBidPblancList", detail="getDmstcCmpetBidPblancDetail"),
    "F": dict(list="getFcltyCmpetBidPblancList", detail="getFcltyCmpetBidPblancDetail"),
}
D2B_URL = "https://www.d2b.go.kr/mainBidAnnounceList.do"


def dt_fmt(v):
    v = re.sub(r"\D", "", v or "")
    return f"{v[:4]}-{v[4:6]}-{v[6:8]} {v[8:10]}:{v[10:12]}" if len(v) >= 12 else (f"{v[:4]}-{v[4:6]}-{v[6:8]}" if len(v) >= 8 else None)


def lmt_names(v):
    """'[12] 부산광역시' · '[4991] 금속창호…' 여러 개 → 이름 목록"""
    return [m.strip(" ,/|") for m in re.findall(r"\]\s*([^\[\]]+)", v or "") if m.strip(" ,/|")]


def notice_key(kind, it):
    if kind == "D":
        return "DB-" + "-".join((it.get(k) or "").strip() for k in ("demandYear", "orntCode", "dcsNo", "pblancNo"))
    return "FB-" + "-".join((it.get(k) or "").strip() for k in ("orntCode", "cntrwkNo", "pblancNo"))


def collect_notices(api, now):
    old = {it["id"]: it for it in load_json(OUT / "bids.json", {}).get("items", [])}
    out = {}
    bgn, end = now.strftime("%Y%m%d"), (now + dt.timedelta(days=60)).strftime("%Y%m%d")
    for kind, K in NOTICE_KINDS.items():
        items, page = [], 1
        while True:
            got, total = api.call(K["list"], svc="BidPblancInfoService", numOfRows=100, pageNo=page, opengDateBegin=bgn, opengDateEnd=end)
            items += got
            if not got or len(items) >= total:
                break
            page += 1
        latest = {}
        for it in items:   # 같은 공고는 마지막 차수만
            k = notice_key(kind, it)
            if int(it.get("pblancOdr") or 0) >= int((latest.get(k) or {}).get("pblancOdr") or 0):
                latest[k] = it
        for k, it in latest.items():
            if "취소" in (it.get("pblancSe") or ""):
                continue
            close = dt_fmt(it.get("biddocPresentnClosDt"))
            if close and close < now.strftime("%Y-%m-%d %H:%M"):
                continue
            prev = old.get(k)
            if prev and prev.get("odr") == it.get("pblancOdr") and prev.get("_d"):
                out[k] = prev
                continue
            try:
                if kind == "D":
                    det, _ = api.call(K["detail"], svc="BidPblancInfoService", demandYear=it.get("demandYear"), orntCode=it.get("orntCode"),
                                      dcsNo=it.get("dcsNo"), pblancNo=it.get("pblancNo"), pblancOdr=it.get("pblancOdr"))
                else:
                    det, _ = api.call(K["detail"], svc="BidPblancInfoService", pblancYear=it.get("pblancYear"), pblancSeCode=it.get("pblancSeCode"),
                                      pblancNo=it.get("pblancNo"), pblancOdr=it.get("pblancOdr"), cntrwkNo=it.get("cntrwkNo"), orntCode=it.get("orntCode"))
            except RuntimeError as e:
                log("  국방 공고 상세 실패", k, e)
                det = []
            d = det[0] if det else {}
            lo, hi = num(d.get("asessRtLwlt")), num(d.get("asessRtUplmt"))
            base = num(it.get("bsicExpt") or it.get("baseAmnt"))
            busi = (it.get("busiDivs") or "").strip()
            rec = {
                "id": k, "src": "국방", "odr": it.get("pblancOdr"), "kind": "공사" if kind == "F" and busi != "용역" else ("용역" if busi == "용역" else "물품"),
                "nm": (it.get("bidNm") or it.get("cntrwkNm") or "").strip(), "org": (it.get("ornt") or "").strip(),
                "cm": it.get("cntrctMth"), "dm": d.get("sucbidrDecsnMth"), "bm": d.get("bidMth"), "se": it.get("pblancSe"),
                "reg": dt_fmt(it.get("bidPartcptRegistClosDt") or d.get("bidPartcptReqstClosDt")), "close": close, "open": dt_fmt(it.get("opengDt")),
                "base": int(base) if base else None, "est": int(num(d.get("estmPrce"))) if num(d.get("estmPrce")) else None,
                "budget": int(num(d.get("budgetAmount"))) if num(d.get("budgetAmount")) else None,
                "rng": [lo, hi] if (lo or hi) else None, "floor": num(d.get("scsbidLwltRt")) or None,
                "rgn": lmt_names(d.get("areaLmttList")),
                # 시설공사 면허는 옛 이름(예: 금속창호ㆍ지붕건축물조립공사업)으로 와서 23개 정식 이름으로 — 물품·용역 업종은 원문 그대로(오매칭 방지)
                "inds": [(normalize_licenses(t) or [t])[0] if kind == "F" else t for t in lmt_names(d.get("lcnsLmttList"))],
                "prd": (d.get("prdlstNm") or "").strip() or None, "g2b": it.get("g2bPblancNo"), "url": D2B_URL, "_d": 1 if det else None,
            }
            out[k] = {kk: v for kk, v in rec.items() if v not in (None, "", [])}
    items = sorted(out.values(), key=lambda x: (x.get("close") or "9999", x["id"]))
    write_if_changed(OUT / "bids.json", dumps({"v": 1, "updated_at": now.isoformat(timespec="minutes"), "items": items}))
    log(f"진행중 국방 공고 {len(items)}건 → d2b/bids.json")


def main():
    t0 = time.time()
    api = Api(t0 + MAX_MINUTES * 60)
    store = Store()
    index = load_json(OUT / "index.json", {"done": {}, "fail": {}})
    meta = load_json(OUT / "meta.json", {})
    if index.get("v") != 2:   # v1(상위 30곳만, 연도 파일) → v2(전원, 월 파일): 처음부터 다시 받는다
        for p in OUT.glob("[0-9][0-9][0-9][0-9].json"):
            p.unlink()
        index, meta = {"v": 2, "done": {}, "fail": {}}, {k: v for k, v in meta.items() if k not in ("backfill", "files", "counts", "total")}
        write_if_changed(OUT / "meta.json", dumps(meta))
        log("v2(참가 업체 전원)로 처음부터 다시 받습니다")
    if not index.get("done"):   # 진행 기록이 없거나 지워졌으면 저장된 결과에서 다시 만든다
        for p in OUT.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9].json"):
            for it in load_json(p, {}).get("items", []):
                index["done"][it["id"]] = 1
        index.setdefault("fail", {})
        if index["done"]:
            log(f"진행 기록 복구: 저장된 결과 {len(index['done'])}건")
    now = now_kst()
    bf = meta.get("backfill") or {}
    changed = 0
    try:
        # ⓪ 진행중 공고 (우리 공고 탭용, 매번)
        try:
            collect_notices(api, now)
        except RuntimeError as e:
            log("국방 공고 수집 실패:", e)
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

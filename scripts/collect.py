#!/usr/bin/env python3
"""조달청 나라장터 공사 입찰·낙찰 데이터 수집기.

GitHub Actions(.github/workflows/collect.yml)에서 매일 실행된다.

환경변수
  DATA_GO_KR_KEY   공공데이터포털 서비스키 (필수, GitHub 시크릿)
  START_DATE       YYYYMMDD. 주면 이 날짜까지 과거 낙찰 목록을 거슬러 올라가며 수집
  RESET_BACKFILL   true 면 과거 수집 커서를 오늘로 되돌려 처음부터 다시 수집
  API_DAILY_LIMIT  서비스(낙찰정보/입찰공고)별 하루 호출 한도. 기본 950
  MAX_MINUTES      이 시간이 지나면 저장하고 종료. 기본 300
  STEPS            실행할 단계 이름(쉼표 구분). 비우면 전부. 예: "공고,최근낙찰"

단계 (이름)
  1) 공고      입찰공고(진행중) 갱신      → data/bids.json, data/notice_cache.json
  2) 최근낙찰  최근 낙찰 목록 갱신        → data/scsbid/{시도}.json
  3) 상세      개찰 전체 순위·복수예가(최신부터, 과거 수집 몫 250회는 남김) → data/opening/{시도}/{연도}.json (regions.json 지역만)
  4) 과거낙찰  과거 낙찰 목록, 최근 24개월까지 먼저 (진행 상황 meta.json)
  5) 상세      남은 한도로 이어서
  6) 지역보강  과거 낙찰 레코드에 참가가능지역(rgn) 채우기 — 달 단위로 최신부터, 입찰공고 호출 REGION_RESERVE 회는 남김
  7) 과거낙찰  나머지 과거(3년까지)

API 필드명이 확정되지 않았으므로 모든 필드 접근은 후보 이름 목록(F_*)을 거친다.
첫 실행 때 각 API 원본 1건을 data/_sample_{op}.json 에 저장하니 그걸 보고 후보를 고친다.
"""
import datetime as dt
import json
import math
import os
import re
import sys
import time
import traceback
import urllib.parse
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from korea import SIDO_ORDER, normalize_licenses, parse_region  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
KST = dt.timezone(dt.timedelta(hours=9))

DAILY_LIMIT = int(os.environ.get("API_DAILY_LIMIT") or 950)
MAX_MINUTES = float(os.environ.get("MAX_MINUTES") or 300)
MAX_FILE_BYTES = 45 * 1024 * 1024          # 파일 하나 50MB 미만 유지
ROWS = 999                                  # 페이지당 건수
NOTICE_CACHE_DAYS = 60                      # 낙찰 정보 보강용 공고 보관 기간
RECENT_SCSBID_DAYS = 40                     # 매일 다시 훑는 최근 낙찰 기간
BACKFILL_YEARS = 3
DETAIL_RESERVE = 250                        # 첫 상세 단계는 낙찰정보 호출을 이만큼 남겨 과거 수집에 쓴다
RECENT_FIRST_MONTHS = 24                    # 과거 낙찰은 이 기간을 먼저 채운 뒤 상세 → 나머지 과거 순으로
DETAIL_MAX_TRIES = 3
REGION_RESERVE = 250                        # 지역보강은 입찰공고 호출을 이만큼 남긴다 (뒤의 과거 수집 몫)
SCHEMA_VERSION = 1

# ---------------------------------------------------------------- API 정의
SERVICES = {
    "scsbid": [  # 낙찰정보서비스
        "https://apis.data.go.kr/1230000/as/ScsbidInfoService",
        "https://apis.data.go.kr/1230000/ScsbidInfoService",
    ],
    "bid": [  # 입찰공고정보서비스
        "https://apis.data.go.kr/1230000/ad/BidPublicInfoService",
        "https://apis.data.go.kr/1230000/BidPublicInfoService05",
        "https://apis.data.go.kr/1230000/BidPublicInfoService04",
    ],
}
OPS = {
    # 키: (서비스, 오퍼레이션, 날짜 범위 조회 시 inqryDiv)
    "scsbid_list": ("scsbid", "getScsbidListSttusCnstwk", "1"),          # 공사 낙찰 목록
    "opening_rank": ("scsbid", "getOpengResultListInfoOpengCompt", None),  # 개찰결과 전체 순위
    "prepar_detail": ("scsbid", "getOpengResultListInfoCnstwkPreparPcDetail", "2"),  # 복수예비가격 상세
    "notice_list": ("bid", "getBidPblancListInfoCnstwk", "1"),           # 공사 입찰공고 목록
    "notice_bsis": ("bid", "getBidPblancListInfoCnstwkBsisAmount", "1"),  # 기초금액·A값
    "notice_license": ("bid", "getBidPblancListInfoLicenseLimit", "1"),   # 면허제한
    "notice_region": ("bid", "getBidPblancListInfoPrtcptPsblRgn", "1"),   # 참가가능지역
}

# ---------------------------------------------------------------- 필드 후보 (앞쪽 우선)
F_NO = ["bidNtceNo"]
F_ORD = ["bidNtceOrd"]
F_NAME = ["bidNtceNm", "cnstwkNm"]
F_NTCE_ORG = ["ntceInsttNm", "ntceInsttNm1"]
F_DMND_ORG = ["dminsttNm", "dmndInsttNm"]
F_NTCE_DT = ["bidNtceDt", "rgstDt", "bidNtceDate"]
F_CLOSE_DT = ["bidClseDt", "bidClseDate"]
F_OPEN_DT = ["rlOpengDt", "opengDt", "opengDate"]
F_KIND = ["ntceKindNm", "bidNtceKindNm"]
F_URL = ["bidNtceDtlUrl", "bidNtceUrl"]
F_SITE = ["cnstrtsiteRgnNm", "cnstrtSiteRgnNm", "cnstwkSiteRgnNm"]
F_MAIN_LIC = ["mainCnsttyNm", "mainCnsttyNm1"]
F_EST = ["presmptPrce", "presmptPrc"]
F_FLOOR = ["sucsfbidLwltRate", "scsbdLwltRate"]
F_BASE = ["bssamt", "bsisAmt", "bssAmt"]
F_RNG_LO = ["rsrvtnPrceRngBgnRate", "rsrvtnPrceRngBgnRt"]
F_RNG_HI = ["rsrvtnPrceRngEndRate", "rsrvtnPrceRngEndRt"]
F_A_TOTAL = ["aValue", "aVal", "aAmt"]
F_A_PARTS = [  # A값 = 국민연금 + 건강보험 + 노인장기요양 + 퇴직급여 + 산업안전보건관리비 + 안전관리비 + 품질관리비
    "npnInsrprm", "mrfnHealthInsrprm", "odsnLngtrmrcprInsrprm", "rtrfundNon",
    "sftyMngcst", "sftyChckMngcst", "qltyMngcst",
]
F_NET = ["pureCnstrctCst", "pureCnstrtnCst", "netCnstrctCst", "cnstrtnAbsltPrc", "pureCnstcst"]
F_LIC = ["lcnsLmtNm", "indstrytyNm", "permsnIndstrytyList"]
F_RGN = ["prtcptPsblRgnNm", "rgnNm"]
F_AMT = ["sucsfbidAmt", "scsbdAmt"]
F_RATE = ["sucsfbidRate", "scsbdRate"]
F_CNT = ["prtcptCnum", "prtcptCnt"]
F_WIN = ["bidwinnrNm", "scsbdrNm"]
F_WIN_BIZ = ["bidwinnrBizno", "scsbdrBizno"]
F_PLAN = ["plnprc", "plnPrc"]
F_RBID = ["rbidNo"]
F_RANK = ["opengRank", "rank"]
F_CORP = ["prcbdrNm", "bidprcCorpNm", "corpNm"]
F_BIZ = ["prcbdrBizno", "bizno", "bizNo"]
F_BID_AMT = ["bidprcAmt", "bidAmt"]
F_BID_RATE = ["bidprcrt", "bidprcRt", "bidRate"]
F_NOTE = ["rmrk", "rmk"]
F_SNO = ["compnoRsrvtnPrceSno", "rsrvtnPrceSno"]
F_PRICE = ["bsisPlnprc", "rsrvtnPrce"]
F_DRAWN = ["drwtYn"]
F_DRAW_CNT = ["drwtNum", "drwtCnt"]


# ---------------------------------------------------------------- 유틸
def now_kst():
    return dt.datetime.now(KST)


def log(*a):
    print(now_kst().strftime("%H:%M:%S"), *a, flush=True)


def pick(d, keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", " "):
            return v
    return None


def pick_fuzzy(d, keys, patterns):
    """후보 이름에 없으면 키 이름 패턴(정규식)으로 찾아본다."""
    v = pick(d, keys)
    if v is not None:
        return v
    for k, val in d.items():
        if val not in (None, "") and any(re.search(p, k, re.I) for p in patterns):
            return val
    return None


def num(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return None if (isinstance(v, float) and math.isnan(v)) else float(v)
    s = str(v).strip().replace(",", "").replace("%", "").replace("원", "")
    if s in ("", "-", "null", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def to_int(v):
    f = num(v)
    return int(round(f)) if f is not None else None


def to_rate(v, nd=4):
    f = num(v)
    return round(f, nd) if f is not None else None


def norm_dt(v):
    """여러 날짜 형식 → 'YYYY-MM-DD HH:MM' (시간 없으면 'YYYY-MM-DD')."""
    if v in (None, ""):
        return None
    s = str(v).strip()
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 12:
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]} {digits[8:10]}:{digits[10:12]}"
    if len(digits) >= 8:
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    return None


def parse_dt(s):
    if not s:
        return None
    try:
        if len(s) >= 16:
            return dt.datetime.strptime(s[:16], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        return dt.datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=KST)
    except ValueError:
        return None


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


def clean(d):
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def split_write(stem, header, items, item_key="items"):
    """items 를 50MB 미만 파일 여러 개로 나눠 쓴다. stem=data/scsbid/강원 → 강원.json, 강원_2.json …
    돌려주는 값: data/ 기준 상대경로 목록."""
    chunks, cur, size = [], [], 0
    for it in items:
        s = len(dumps(it).encode("utf-8")) + 1
        if cur and size + s > MAX_FILE_BYTES:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(it)
        size += s
    chunks.append(cur)
    paths = []
    for i, ch in enumerate(chunks):
        p = stem.with_name(stem.name + ("" if i == 0 else f"_{i + 1}") + ".json")
        body = dict(header, part=i + 1, parts=len(chunks))
        body[item_key] = ch
        write_if_changed(p, dumps(body))
        paths.append(p.relative_to(DATA).as_posix())
    # 예전에 더 많이 나뉘어 있던 조각 정리
    i = len(chunks) + 1
    while True:
        old = stem.with_name(f"{stem.name}_{i}.json")
        if not old.exists():
            break
        old.unlink()
        i += 1
    return paths


def read_split(stem, item_key="items"):
    out = []
    first = load_json(stem.with_name(stem.name + ".json"), None)
    if not first:
        return out
    out.extend(first.get(item_key) or [])
    for i in range(2, (first.get("parts") or 1) + 1):
        part = load_json(stem.with_name(f"{stem.name}_{i}.json"), {})
        out.extend(part.get(item_key) or [])
    return out


# ---------------------------------------------------------------- API 클라이언트
class BudgetExhausted(Exception):
    pass


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"{code}: {msg}")
        self.code, self.msg = code, msg


class FatalApiError(Exception):
    pass


class Api:
    def __init__(self, key, meta, deadline):
        key = key.strip()
        self.key = urllib.parse.unquote(key) if "%" in key else key  # 인코딩 키를 넣어도 동작
        self.meta = meta
        self.deadline = deadline
        api = meta.setdefault("api", {})
        self.bases = api.setdefault("base", {})
        today = now_kst().strftime("%Y%m%d")
        calls = api.get("calls") or {}
        if calls.get("date") != today:
            calls = {"date": today}
        api["calls"] = calls
        self.calls = calls
        self.errors = []
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "bid-predictor-collector/1.0"

    def remaining(self, svc):
        return DAILY_LIMIT - self.calls.get(svc, 0)

    def time_left(self):
        return (self.deadline - time.time()) / 60

    def call(self, op, params):
        svc, opname, _ = OPS[op]
        if self.remaining(svc) <= 0:
            raise BudgetExhausted(svc)
        if time.time() > self.deadline:
            raise BudgetExhausted("time")
        bases = SERVICES[svc]
        known = self.bases.get(svc)
        if known in bases:
            bases = [known] + [b for b in bases if b != known]
        last_err = None
        for base in bases:
            url = f"{base}/{opname}"
            q = {"serviceKey": self.key, "type": "json", **params}
            resp = None
            for attempt in range(4):
                try:
                    resp = self.session.get(url, params=q, timeout=60)
                    break
                except requests.RequestException as e:
                    last_err = e
                    time.sleep(2 ** attempt)
            if resp is None:
                continue
            self.calls[svc] = self.calls.get(svc, 0) + 1
            text = resp.text or ""
            if any(s in text for s in ("SERVICE_KEY_IS_NOT_REGISTERED", "SERVICE KEY IS NOT REGISTERED",
                                        "SERVICE_ACCESS_DENIED", "UNREGISTERED_IP")):
                raise FatalApiError(f"{opname}: 서비스키 오류/미승인 ({_xml_msg(text)})")
            if "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS" in text or "요청 제한" in text:
                self.calls[svc] = DAILY_LIMIT
                raise BudgetExhausted(svc)
            if resp.status_code in (404, 500, 502, 503) or not text.strip().startswith(("{", "[")):
                last_err = f"HTTP {resp.status_code} {_xml_msg(text)[:120]}"
                log(f"  ! {url} → {last_err}")
                continue
            try:
                j = resp.json()
            except ValueError as e:
                last_err = e
                continue
            items, total = self._parse(j, op)
            self.bases[svc] = base
            if items:
                self._save_sample(op, url, params, j, items)
            return items, total
        raise ApiError("NET", f"{opname}: {last_err}")

    @staticmethod
    def _parse(j, op):
        root = j.get("response", j) if isinstance(j, dict) else {}
        header = root.get("header") or {}
        if not header:  # 오류 응답이 다른 이름의 최상위 키로 올 때
            for v in (j.values() if isinstance(j, dict) else []):
                if isinstance(v, dict) and "resultCode" in json.dumps(v):
                    header = v.get("header", v)
                    break
        code = str(header.get("resultCode", "00"))
        if code not in ("00", "0", "000", "INFO-000"):
            if code in ("03", "INFO-200"):  # 데이터 없음
                return [], 0
            raise ApiError(code, header.get("resultMsg", ""))
        body = root.get("body") or {}
        items = body.get("items")
        if isinstance(items, dict):
            items = items.get("item", items)
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            items = []
        items = [i for i in items if isinstance(i, dict)]
        try:
            total = int(body.get("totalCount") or 0)
        except (TypeError, ValueError):
            total = len(items)
        return items, total

    def _save_sample(self, op, url, params, raw, items):
        path = DATA / f"_sample_{op}.json"
        if path.exists():
            return
        body = {
            "endpoint": url,
            "params": {k: v for k, v in params.items() if k != "serviceKey"},
            "saved_at": now_kst().isoformat(timespec="seconds"),
            "fields": sorted(items[0].keys()),
            "item": items[0],
            "raw_header": (raw.get("response") or {}).get("header") if isinstance(raw, dict) else None,
        }
        path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"  ✎ 샘플 저장 {path.name}")

    def paged(self, op, params, rows=ROWS):
        page, got = 1, 0
        while True:
            try:
                items, total = self.call(op, dict(params, pageNo=page, numOfRows=rows))
            except ApiError as e:
                if page == 1 and rows > 100 and ("numOfRows" in e.msg or e.code in ("10", "11")):
                    rows = 100
                    continue
                raise
            yield from items
            got += len(items)
            if not items or got >= total or len(items) < rows:
                return
            page += 1

    def fetch_range(self, op, bgn, end, extra=None):
        """날짜 범위 조회. 범위가 너무 크다는 오류면 반씩 나눠 다시 시도."""
        div = OPS[op][2]
        params = {"inqryDiv": div, "inqryBgnDt": bgn.strftime("%Y%m%d%H%M"),
                  "inqryEndDt": end.strftime("%Y%m%d%H%M"), **(extra or {})}
        try:
            return list(self.paged(op, params))
        except ApiError as e:
            span = end - bgn
            if span > dt.timedelta(days=1) and (e.code in ("07", "08", "99") or "범위" in e.msg or "기간" in e.msg):
                mid = bgn + span / 2
                log(f"  ↳ {op} 범위 분할 ({e})")
                return self.fetch_range(op, bgn, mid, extra) + self.fetch_range(op, mid, end, extra)
            raise


def _xml_msg(text):
    m = re.search(r"<returnAuthMsg>(.*?)</returnAuthMsg>", text) or re.search(r"<resultMsg>(.*?)</resultMsg>", text)
    return m.group(1) if m else text[:200].replace("\n", " ")


# ---------------------------------------------------------------- 공고 정보
def notice_id(it):
    no = pick(it, F_NO)
    if not no:
        return None, None, None
    ord_ = str(pick(it, F_ORD) or "0").strip()
    return f"{no}-{ord_}", str(no).strip(), ord_


def a_value(it):
    total = num(pick(it, F_A_TOTAL))
    if total is not None:
        return int(total)
    parts = [num(it.get(k)) for k in F_A_PARTS]
    parts = [p for p in parts if p is not None]
    return int(sum(parts)) if parts else None


def net_cost(it):
    return to_int(pick_fuzzy(it, F_NET, [r"^pure", r"순공사"]))


def price_range(it):
    """예가범위 [하한%, 상한%] 예: [-2, 2]. 부호 없이 오는 하한은 음수로 맞춘다."""
    lo, hi = num(pick(it, F_RNG_LO)), num(pick(it, F_RNG_HI))
    if lo is None and hi is None:
        return None
    if lo is not None and lo > 0:
        lo = -lo
    return [lo, hi]


class NoticeCache:
    """진행중·최근 공고. 낙찰 정보 보강과 bids.json 생성에 쓴다."""
    path = DATA / "notice_cache.json"

    def __init__(self, items=None):
        if items is None:
            raw = load_json(self.path, {})
            items = raw.get("items", {}) if isinstance(raw, dict) else {}
        self.items = items

    def get(self, id_):
        e = self.items.get(id_)
        if e is None:
            e = self.items[id_] = {"id": id_}
            no, _, ord_ = id_.rpartition("-")
            e["no"], e["ord"] = no, ord_
        return e

    def lookup(self, no, ord_):
        e = self.items.get(f"{no}-{ord_}")
        if e and e.get("nm"):
            return e
        cands = [v for v in self.items.values() if v.get("no") == no and v.get("nm")]
        return max(cands, key=lambda v: v.get("ord", "")) if cands else None

    def add_notice(self, it, seen):
        id_, no, ord_ = notice_id(it)
        if not id_:
            return
        e = self.get(id_)
        if seen:
            e.setdefault("seen", seen)
        kind = pick(it, F_KIND) or ""
        site = pick(it, F_SITE)
        e.update(clean({
            "nm": (pick(it, F_NAME) or "").strip(),
            "org": pick(it, F_NTCE_ORG),
            "dmd": pick(it, F_DMND_ORG),
            "ntce": norm_dt(pick(it, F_NTCE_DT)),
            "close": norm_dt(pick(it, F_CLOSE_DT)),
            "open": norm_dt(pick(it, F_OPEN_DT)),
            "est": to_int(pick(it, F_EST)),
            "floor": to_rate(pick(it, F_FLOOR), 3),
            "url": pick(it, F_URL),
            "site": site,
            "lic_raw": pick(it, F_MAIN_LIC),
        }))
        base = to_int(pick(it, F_BASE))
        if base:
            e["base"] = base
        if "취소" in kind:
            e["cancel"] = True
        # 변경공고(차수 증가)가 오면 이전 차수는 대체된 것으로 표시
        for v in self.items.values():
            if v.get("no") == no and v.get("ord", "") < ord_:
                v["old"] = True

    def add_bsis(self, it):
        id_, _, _ = notice_id(it)
        if not id_:
            return
        e = self.get(id_)
        e.update(clean({
            "base": to_int(pick(it, F_BASE)),
            "a": a_value(it),
            "net": net_cost(it),
            "rng": price_range(it),
        }))

    def add_license(self, it):
        id_, _, _ = notice_id(it)
        if not id_:
            return
        e = self.get(id_)
        raw = pick(it, F_LIC)
        if raw:
            lst = e.setdefault("lic_list", [])
            if raw not in lst:
                lst.append(raw)

    def add_region(self, it):
        id_, _, _ = notice_id(it)
        if not id_:
            return
        e = self.get(id_)
        r = pick(it, F_RGN)
        if r:
            lst = e.setdefault("rgn", [])
            if r not in lst:
                lst.append(r)

    def finalize(self, now):
        """지역·면허 계산, 오래된 항목 삭제."""
        cutoff = (now - dt.timedelta(days=NOTICE_CACHE_DAYS)).strftime("%Y-%m-%d")
        for id_ in list(self.items):
            e = self.items[id_]
            ref = e.get("close") or e.get("ntce") or e.setdefault("seen", now.isoformat(timespec="minutes"))
            if ref[:10] < cutoff:
                del self.items[id_]
                continue
            finish_notice(e)

    def save(self):
        write_if_changed(self.path, dumps({"items": self.items}))

    def bids(self, now):
        cur = now.strftime("%Y-%m-%d %H:%M")
        recent = (now - dt.timedelta(days=14)).strftime("%Y-%m-%d")
        out = []
        for e in self.items.values():
            if not e.get("nm") or e.get("cancel") or e.get("old"):
                continue
            close = e.get("close")
            if close and close < cur:
                continue
            if not close and (e.get("ntce") or "") < recent:
                continue
            out.append(clean({k: e.get(k) for k in (
                "id", "no", "ord", "nm", "org", "dmd", "sido", "sgg", "lic", "rgn", "base", "est",
                "floor", "a", "net", "rng", "ntce", "close", "open", "url", "seen")}))
        out.sort(key=lambda x: (x.get("close") or "9999", x["id"]))
        return out


def write_lic_map(cache):
    """앱 실시간 검색용: 최근 60일 공사 공고별 면허제한·참가가능지역 → data/lic_map.json
    {"v":1, "lic":[면허명…], "items":{공고ID:[면허 번호…]}, "rg":[지역 원문…], "rgn":{공고ID:[지역 번호…]}}
    (조달청 검색조건의 업종 필터가 0건을 돌려주는 문제 대신 + 실시간 공고엔 면허제한·참가가능지역이 없어 '참가 가능' 판정에 씀)"""
    names, index, items = [], {}, {}
    rnames, rindex, rgns = [], {}, {}

    def ids(values, nm, ix):
        out = []
        for v in values:
            if v not in ix:
                ix[v] = len(nm)
                nm.append(v)
            out.append(ix[v])
        return out

    for id_, e in sorted(cache.items.items()):
        if not e.get("nm"):
            continue
        if e.get("lic"):
            items[id_] = ids(e["lic"], names, index)
        if e.get("rgn"):
            rgns[id_] = ids(e["rgn"], rnames, rindex)
    return write_if_changed(DATA / "lic_map.json", dumps({"v": SCHEMA_VERSION, "lic": names, "items": items, "rg": rnames, "rgn": rgns}))


def finish_notice(e):
    rgn = e.get("rgn") or []
    single_rgn = rgn[0] if len(rgn) == 1 else None
    sido, sgg = parse_region(e.get("site"), single_rgn, e.get("dmd"), e.get("org"))
    if sido:
        e["sido"] = sido
    if sgg:
        e["sgg"] = sgg
    lic = normalize_licenses(e.get("lic_list") or [], None if e.get("lic_list") else e.get("lic_raw"))
    if lic:
        e["lic"] = lic


# ---------------------------------------------------------------- 낙찰 목록
SCSBID_FIELDS = ("id", "no", "ord", "nm", "org", "dmd", "sido", "sgg", "lic", "base", "plan", "amt",
                 "rate", "cnt", "floor", "a", "net", "rng", "rgn", "date", "win", "winBiz", "sr")


def compute_sr(r):
    """사정율 = 예정가격 / 기초금액 × 100. 예정가격이 없으면 낙찰금액 ÷ 낙찰률로 역산."""
    plan = r.get("plan")
    if not plan and r.get("amt") and r.get("rate"):
        plan = round(r["amt"] / (r["rate"] / 100))
        r["plan"] = plan
    if plan and r.get("base"):
        sr = plan / r["base"] * 100
        r["sr"] = round(sr, 4) if 80 < sr < 120 else None
    if r.get("sr") is None:
        r.pop("sr", None)


class ScsbidStore:
    dir = DATA / "scsbid"

    def __init__(self):
        self.recs = {}
        self.dirty = False
        for p in sorted(self.dir.glob("*.json")) if self.dir.exists() else []:
            if re.search(r"_\d+$", p.stem):
                continue
            for r in read_split(p.with_suffix("")):
                self.recs[r["id"]] = r
        self.by_no = {}
        for r in self.recs.values():
            self.by_no.setdefault(r["no"], []).append(r["id"])

    def merge(self, it, cache):
        id_, no, ord_ = notice_id(it)
        if not id_:
            return
        rbid = to_int(pick(it, F_RBID)) or 0
        r = self.recs.get(id_)
        if r and rbid < r.get("_rbid", 0):
            return
        new = r is None
        r = r or {"id": id_, "no": no, "ord": ord_}
        before = dumps(r)
        r.update(clean({
            "nm": (pick(it, F_NAME) or "").strip(),
            "org": pick(it, F_NTCE_ORG),
            "dmd": pick(it, F_DMND_ORG),
            "amt": to_int(pick(it, F_AMT)),
            "rate": to_rate(pick(it, F_RATE), 4),
            "cnt": to_int(pick(it, F_CNT)),
            "win": pick(it, F_WIN),
            "winBiz": re.sub(r"\D", "", str(pick(it, F_WIN_BIZ) or "")) or None,
            "date": (norm_dt(pick(it, F_OPEN_DT)) or "")[:10] or None,
            "base": to_int(pick(it, F_BASE)),
            "plan": to_int(pick(it, F_PLAN)),
            "floor": to_rate(pick(it, F_FLOOR), 3),
        }))
        if rbid:
            r["_rbid"] = rbid
        if not r.get("date"):
            return
        n = cache.lookup(no, ord_) if cache else None
        if n:
            self.enrich(r, n)
        if not r.get("sido"):
            sido, sgg = parse_region(r.get("dmd"), r.get("org"))
            if sido:
                r["sido"], r["sgg"] = sido, sgg
        compute_sr(r)
        if new:
            self.recs[id_] = r
            self.by_no.setdefault(no, []).append(id_)
        if new or dumps(r) != before:
            self.dirty = True

    def enrich(self, r, n):
        """공고 정보(n)로 낙찰 레코드(r)를 채운다. 공고 쪽 값이 있으면 우선."""
        before = dumps(r)
        for k in ("org", "dmd", "base", "floor", "a", "net", "rng", "lic", "rgn"):
            if n.get(k) not in (None, "", []):
                r[k] = n[k]
        if n.get("sido"):
            r["sido"] = n["sido"]
            if n.get("sgg"):
                r["sgg"] = n["sgg"]
        compute_sr(r)
        if dumps(r) != before:
            self.dirty = True

    def enrich_by_notice(self, n):
        ids = self.by_no.get(n.get("no"), [])
        for id_ in ids:
            r = self.recs[id_]
            if r.get("ord") == n.get("ord") or len(ids) == 1:
                self.enrich(r, n)

    def add_region(self, it):
        """참가가능지역 API 한 행 → 같은 공고번호(차수)의 낙찰 레코드 rgn 에 추가"""
        _, no, ord_ = notice_id(it)
        r_ = pick(it, F_RGN)
        ids = self.by_no.get(no, []) if r_ else []
        for id_ in ids:
            r = self.recs[id_]
            if r.get("ord") == ord_ or len(ids) == 1:
                lst = r.setdefault("rgn", [])
                if r_ not in lst:
                    lst.append(r_)
                    self.dirty = True

    def save(self):
        files, counts = {}, {}
        groups = {}
        for r in self.recs.values():
            groups.setdefault(r.get("sido") or "기타", []).append(r)
        for sido, recs in groups.items():
            recs.sort(key=lambda r: (r.get("date") or "", r["id"]), reverse=True)
            items = [clean({k: r.get(k) for k in SCSBID_FIELDS}) for r in recs]
            files[sido] = split_write(self.dir / sido, {"sido": sido, "v": SCHEMA_VERSION}, items)
            counts[sido] = len(items)
        return files, counts


# ---------------------------------------------------------------- 개찰 상세 (전체 순위 + 복수예가)
class OpeningStore:
    """data/opening/{시도}/{연도}.json

    파일 형식: {"sido","year","corps":[[업체명,사업자번호],...],
               "bids":{공고ID:{"date","plan","base","p":[[번호,가격,추첨(0/1),추첨횟수],...],
                              "r":[[순위,업체idx,투찰금액,투찰률,비고?],...]}}}
    index.json: {"done":{공고ID:연도}, "fail":{공고ID:시도횟수}}
    """
    dir = DATA / "opening"

    def __init__(self):
        self.years = {}   # (sido, year) -> {"corps":[], "cidx":{}, "bids":{}}
        self.index = {}
        self.dirty = set()

    def idx(self, sido):
        if sido not in self.index:
            self.index[sido] = load_json(self.dir / sido / "index.json", {"done": {}, "fail": {}})
            self.index[sido].setdefault("done", {})
            self.index[sido].setdefault("fail", {})
        return self.index[sido]

    def year(self, sido, year):
        key = (sido, year)
        if key not in self.years:
            y = {"corps": [], "cidx": {}, "bids": {}}
            stem = self.dir / sido / year
            first = load_json(stem.with_suffix(".json"), None)
            parts = [first] if first else []
            for i in range(2, ((first or {}).get("parts") or 1) + 1):
                parts.append(load_json(stem.with_name(f"{year}_{i}.json"), {}))
            for part in parts:
                corps = part.get("corps") or []
                for bid_id, b in (part.get("bids") or {}).items():
                    b["r"] = [[row[0], self._corp(y, *corps[row[1]])] + row[2:] for row in b.get("r", [])]
                    y["bids"][bid_id] = b
            self.years[key] = y
        return self.years[key]

    @staticmethod
    def _corp(y, name, biz):
        k = biz or name
        if k not in y["cidx"]:
            y["cidx"][k] = len(y["corps"])
            y["corps"].append([name, biz])
        return y["cidx"][k]

    def add(self, sido, rec, ranks, prices):
        year = rec["date"][:4]
        y = self.year(sido, year)
        rows = []
        for it in ranks:
            rank = to_int(pick(it, F_RANK))
            name = (pick(it, F_CORP) or "").strip()
            biz = re.sub(r"\D", "", str(pick(it, F_BIZ) or ""))
            amt = to_int(pick(it, F_BID_AMT))
            if not (name or biz) or amt is None:
                continue
            row = [rank if rank is not None else 0, self._corp(y, name, biz), amt, to_rate(pick(it, F_BID_RATE), 4)]
            note = (pick(it, F_NOTE) or "").strip()
            if note:
                row.append(note[:30])
            rows.append(row)
        rows.sort(key=lambda r: (r[0] <= 0, r[0]))
        p = []
        for it in prices:
            sno = to_int(pick(it, F_SNO))
            price = to_int(pick(it, F_PRICE))
            if sno is None or price is None:
                continue
            drawn = 1 if str(pick(it, F_DRAWN) or "").upper() in ("Y", "1", "TRUE") else 0
            p.append([sno, price, drawn, to_int(pick(it, F_DRAW_CNT)) or 0])
        p.sort()
        plan = next((to_int(pick(it, F_PLAN)) for it in prices if pick(it, F_PLAN)), None)
        base = next((to_int(pick(it, F_BASE)) for it in prices if pick(it, F_BASE)), None)
        y["bids"][rec["id"]] = clean({"date": rec["date"], "plan": plan or rec.get("plan"),
                                      "base": base or rec.get("base"), "p": p, "r": rows})
        self.idx(sido)["done"][rec["id"]] = year
        self.idx(sido)["fail"].pop(rec["id"], None)
        self.dirty.add((sido, year))
        return plan, base

    def fail(self, sido, rec_id):
        f = self.idx(sido)["fail"]
        f[rec_id] = f.get(rec_id, 0) + 1

    def save(self):
        for (sido, year) in sorted(self.dirty):
            y = self.years[(sido, year)]
            # 파일 조각마다 업체 표를 따로 만든다
            ids = sorted(y["bids"], key=lambda i: (y["bids"][i].get("date", ""), i))
            chunks, cur, size = [], [], 0
            for i in ids:
                s = len(dumps(y["bids"][i])) + 40
                if cur and size + s > MAX_FILE_BYTES:
                    chunks.append(cur)
                    cur, size = [], 0
                cur.append(i)
                size += s
            chunks.append(cur)
            stem = self.dir / sido / year
            for n, ch in enumerate(chunks):
                corps, cidx, bids = [], {}, {}
                for i in ch:
                    b = dict(y["bids"][i])
                    rows = []
                    for row in b.get("r", []):
                        name, biz = y["corps"][row[1]]
                        k = biz or name
                        if k not in cidx:
                            cidx[k] = len(corps)
                            corps.append([name, biz])
                        rows.append([row[0], cidx[k]] + row[2:])
                    b["r"] = rows
                    bids[i] = b
                p = stem.with_name(year + ("" if n == 0 else f"_{n + 1}") + ".json")
                write_if_changed(p, dumps({"sido": sido, "year": year, "part": n + 1, "parts": len(chunks),
                                           "corps": corps, "bids": bids}))
            k = len(chunks) + 1
            while stem.with_name(f"{year}_{k}.json").exists():
                stem.with_name(f"{year}_{k}.json").unlink()
                k += 1
        for sido, ix in self.index.items():
            write_if_changed(self.dir / sido / "index.json", dumps(ix))
        self.dirty.clear()

    def files(self):
        out, counts = {}, {}
        if not self.dir.exists():
            return out, counts
        for sd in sorted(self.dir.iterdir()):
            if not sd.is_dir():
                continue
            years = {}
            for p in sorted(sd.glob("*.json")):
                m = re.fullmatch(r"(\d{4})(?:_\d+)?", p.stem)
                if m:
                    years.setdefault(m.group(1), []).append(p.relative_to(DATA).as_posix())
            for y in years:
                years[y].sort(key=lambda s: (len(s), s))
            out[sd.name] = years
            counts[sd.name] = len(self.idx(sd.name)["done"])
        return out, counts


# ---------------------------------------------------------------- 단계별 작업
def step_notices(api, meta, cache, now):
    last = parse_dt((meta.get("notice_last") or "")[:16].replace("T", " "))
    days = 30 if not cache.items or not last else min(30, max(2, (now - last).days + 2))
    bgn = now - dt.timedelta(days=days)
    seen = now.isoformat(timespec="minutes")
    log(f"[공고] 최근 {days}일")
    for it in api.fetch_range("notice_list", bgn, now):
        cache.add_notice(it, seen)
    for it in api.fetch_range("notice_bsis", now - dt.timedelta(days=max(days, 14)), now):
        cache.add_bsis(it)
    for it in api.fetch_range("notice_license", bgn, now):
        cache.add_license(it)
    for it in api.fetch_range("notice_region", bgn, now):
        cache.add_region(it)
    cache.finalize(now)
    meta["notice_last"] = seen


def step_recent_scsbid(api, meta, store, cache, now):
    log(f"[낙찰] 최근 {RECENT_SCSBID_DAYS}일")
    bgn = now - dt.timedelta(days=RECENT_SCSBID_DAYS)
    n = 0
    for it in api.fetch_range("scsbid_list", bgn, now):
        store.merge(it, cache)
        n += 1
    # 캐시에 있는 공고로 기존 레코드 보강
    for e in cache.items.values():
        if e.get("nm"):
            store.enrich_by_notice(e)
    log(f"  낙찰 {n}건 조회")


def step_backfill(api, meta, store, now, checkpoint, horizon=None):
    """horizon 을 주면 커서가 그 날짜보다 과거로 가기 전에 멈춘다 (최근분 우선 수집용)."""
    bf = meta.setdefault("backfill", {})
    start_env = (os.environ.get("START_DATE") or "").strip()
    if os.environ.get("RESET_BACKFILL", "").lower() in ("1", "true", "yes"):
        bf["cursor"] = None
        bf["done"] = False
    if re.fullmatch(r"\d{8}", start_env):
        if start_env < bf.get("target_start", "99999999") or bf.get("done") is not True:
            bf["done"] = False
        bf["target_start"] = start_env
    bf.setdefault("target_start", (now - dt.timedelta(days=365 * BACKFILL_YEARS)).strftime("%Y%m%d"))
    if bf.get("done"):
        return
    target = dt.datetime.strptime(bf["target_start"], "%Y%m%d").replace(tzinfo=KST)
    cursor = bf.get("cursor") or now.strftime("%Y%m%d")
    while True:
        cur = dt.datetime.strptime(cursor, "%Y%m%d").replace(tzinfo=KST)
        if cur < target:
            bf["done"] = True
            bf["cursor"] = None
            log("[과거] 완료")
            return
        if horizon and cur < horizon:
            log(f"[과거] 최근 {RECENT_FIRST_MONTHS}개월 수집 완료, 커서 {cursor}")
            return
        if api.remaining("scsbid") < 40 or api.remaining("bid") < 80 or api.time_left() < 20:
            log(f"[과거] 오늘 한도 도달, 커서 {cursor}")
            return
        end = cur.replace(hour=23, minute=59)
        bgn = max(cur.replace(day=1, hour=0, minute=0), target)
        log(f"[과거] {bgn:%Y-%m-%d} ~ {end:%Y-%m-%d}")
        cnt = 0
        for it in api.fetch_range("scsbid_list", bgn, end):
            store.merge(it, None)
            cnt += 1
        # 같은 기간 공고로 기초금액·A값·면허·지역 보강 (과거로 가므로 개찰이 뒤인 건도 이미 저장돼 있음)
        month = NoticeCache({})
        for it in api.fetch_range("notice_list", bgn, end):
            if notice_id(it)[1] in store.by_no:
                month.add_notice(it, None)
        for it in api.fetch_range("notice_bsis", bgn, end):
            if notice_id(it)[0] in month.items:
                month.add_bsis(it)
        for it in api.fetch_range("notice_license", bgn, end):
            if notice_id(it)[0] in month.items:
                month.add_license(it)
        for it in api.fetch_range("notice_region", bgn, end):
            store.add_region(it)
        for e in month.items.values():
            finish_notice(e)
            store.enrich_by_notice(e)
        cursor = bf["cursor"] = (bgn - dt.timedelta(days=1)).strftime("%Y%m%d")
        bf["months_done"] = bf.get("months_done", 0) + 1
        bf["oldest"] = bgn.strftime("%Y%m%d")
        log(f"  낙찰 {cnt}건, 공고 보강 {len(month.items)}건")
        checkpoint()


def step_region_fill(api, meta, store, now, checkpoint):
    """과거 낙찰 레코드에 참가가능지역(rgn)을 채운다. 참가수 예측에 쓰임(시·군 제한이면 참가가 적다).
    공고 게시 달 단위로 최신 → 가장 오래된 낙찰 달까지 한 번 훑는다. 진행: meta.rgn_fill {cursor: YYYYMM, done}"""
    rf = meta.setdefault("rgn_fill", {})
    if rf.get("done"):
        return
    dates = [r["date"] for r in store.recs.values() if r.get("date")]
    if not dates:
        return
    d0 = dt.date.fromisoformat(sorted(dates)[len(dates) // 500])   # 드문 아주 옛 레코드는 무시
    oldest = (d0.replace(day=1) - dt.timedelta(days=1)).strftime("%Y%m")   # 개찰 한 달 전 게시 공고까지
    cursor = rf.get("cursor") or now.strftime("%Y%m")
    while True:
        if cursor < oldest:
            rf["done"], rf["cursor"] = True, None
            log("[지역보강] 완료")
            return
        if api.remaining("bid") < REGION_RESERVE or api.time_left() < 20:
            log(f"[지역보강] 오늘 몫 끝, 커서 {cursor}")
            return
        bgn = dt.datetime.strptime(cursor + "01", "%Y%m%d").replace(tzinfo=KST)
        end = min((bgn + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(minutes=1), now)
        log(f"[지역보강] {bgn:%Y-%m}")
        n = 0
        for it in api.fetch_range("notice_region", bgn, end):
            store.add_region(it)
            n += 1
        cursor = rf["cursor"] = (bgn - dt.timedelta(days=1)).strftime("%Y%m")
        log(f"  지역 {n}행")
        checkpoint()


def step_details(api, meta, store, ostore, regions, now, checkpoint, reserve=3):
    """reserve: 낙찰정보 서비스 호출을 이만큼 남기고 멈춘다 (뒤에 과거 수집이 쓸 몫)"""
    target = meta.get("backfill", {}).get("target_start") or (now - dt.timedelta(days=365 * BACKFILL_YEARS)).strftime("%Y%m%d")
    tdate = f"{target[:4]}-{target[4:6]}-{target[6:8]}"
    dm = meta.setdefault("detail", {})
    dm["regions"] = regions
    queue = []
    for sido in regions:
        ix = ostore.idx(sido)
        recs = [r for r in store.recs.values() if r.get("sido") == sido and (r.get("date") or "") >= tdate]
        pending = [r for r in recs if r["id"] not in ix["done"] and ix["fail"].get(r["id"], 0) < DETAIL_MAX_TRIES]
        dm[sido] = {"total": len(recs), "done": sum(1 for r in recs if r["id"] in ix["done"]),
                    "failed": sum(1 for r in recs if ix["fail"].get(r["id"], 0) >= DETAIL_MAX_TRIES)}
        queue += [(sido, r) for r in pending]
    queue.sort(key=lambda x: x[1]["date"], reverse=True)
    log(f"[상세] 대기 {len(queue)}건")
    done_now = 0
    for sido, rec in queue:
        if api.remaining("scsbid") < reserve or api.time_left() < 10:
            break
        try:
            ranks = list(api.paged("opening_rank", {"bidNtceNo": rec["no"], "bidNtceOrd": rec["ord"]}))
            prices = list(api.paged("prepar_detail", {"inqryDiv": "2", "bidNtceNo": rec["no"]}))
        except ApiError as e:
            log(f"  ! {rec['id']} {e}")
            ostore.fail(sido, rec["id"])
            continue
        # 재입찰이 섞여 오면 마지막 재입찰만
        for lst in (ranks, prices):
            rb = [to_int(pick(i, F_RBID)) or 0 for i in lst]
            if rb and max(rb) > 0:
                lst[:] = [i for i, n in zip(lst, rb) if n == max(rb)]
        prices = [p for p in prices if str(pick(p, F_ORD) or rec["ord"]) == rec["ord"]] or prices
        if not ranks and not prices:
            ostore.fail(sido, rec["id"])
            continue
        plan, base = ostore.add(sido, rec, ranks, prices)
        if plan:
            rec["plan"] = plan
        if base and not rec.get("base"):
            rec["base"] = base
        compute_sr(rec)
        store.dirty = True
        dm[sido]["done"] += 1
        done_now += 1
        if done_now % 100 == 0:
            checkpoint()
    hist = (dm.get("history") or [])[-6:]
    today = now.strftime("%Y%m%d")
    if done_now:
        if dm.get("history_date") == today and hist:
            hist[-1] += done_now          # 하루에 두 번 돌면 합친다
        else:
            hist.append(done_now)
        dm["history_date"] = today
    dm["history"] = hist
    per_day = max(hist) if hist else max(1, (DAILY_LIMIT - 60) // 2)
    remaining = sum(max(0, dm[s]["total"] - dm[s]["done"] - dm[s]["failed"]) for s in regions)
    dm["remaining"] = remaining
    dm["per_day"] = per_day
    dm["eta_days"] = math.ceil(remaining / per_day) if per_day else None
    log(f"  이번 실행 {done_now}건, 남은 {remaining}건 (약 {dm['eta_days']}일)")


# ---------------------------------------------------------------- main
def main():
    started = time.time()
    now = now_kst()
    key = os.environ.get("DATA_GO_KR_KEY", "")
    DATA.mkdir(exist_ok=True)
    meta = load_json(DATA / "meta.json", {})
    regions = [r for r in load_json(ROOT / "scripts" / "regions.json", ["강원"]) if r in SIDO_ORDER]
    if not key.strip():
        log("DATA_GO_KR_KEY 시크릿이 없습니다. 저장소 Settings → Secrets and variables → Actions 에 등록하세요.")
        sys.exit(1)

    api = Api(key, meta, started + MAX_MINUTES * 60)
    cache = NoticeCache()
    store = ScsbidStore()
    ostore = OpeningStore()
    errors = []

    def save_all(final=False):
        cache.save()
        ostore.save()
        files_s, counts_s = store.save()
        files_o, counts_o = ostore.files()
        bids = cache.bids(now)
        changed = write_if_changed(DATA / "bids.json", dumps({"items": bids, "v": SCHEMA_VERSION}))
        changed = write_lic_map(cache) or changed
        old_files = meta.get("files", {})
        meta["files"] = {"scsbid": files_s, "opening": files_o}
        meta["counts"] = {"bids": len(bids), "scsbid": counts_s, "opening": counts_o,
                          "scsbid_total": sum(counts_s.values())}
        meta["v"] = SCHEMA_VERSION
        if changed or store.dirty or ostore.dirty or old_files != meta["files"]:
            meta["updated_at"] = now_kst().isoformat(timespec="seconds")
        meta["last_run"] = {"at": now_kst().isoformat(timespec="seconds"),
                            "minutes": round((time.time() - started) / 60, 1),
                            "calls": dict(api.calls), "errors": errors[-20:]}
        write_if_changed(DATA / "meta.json", json.dumps(meta, ensure_ascii=False, indent=1, sort_keys=True))
        store.dirty = False
        if final:
            log(f"저장 완료: 공고 {len(bids)}건, 낙찰 {sum(counts_s.values())}건, 상세 {sum(counts_o.values())}건")

    # 순서: 공고 → 최근 낙찰 → 개찰 상세(최신부터, 과거 수집 몫은 남김) → 과거 24개월 → 남은 한도로 상세 → 나머지 과거
    # 상세는 낙찰정보 서비스만 쓰고 과거 수집은 주로 입찰공고 서비스를 써서, 같이 돌려도 서로 크게 방해하지 않는다
    horizon = now - dt.timedelta(days=round(30.44 * RECENT_FIRST_MONTHS))
    steps = [
        ("공고", lambda: step_notices(api, meta, cache, now)),
        ("최근낙찰", lambda: step_recent_scsbid(api, meta, store, cache, now)),
        ("상세", lambda: step_details(api, meta, store, ostore, regions, now, save_all, reserve=DETAIL_RESERVE)),
        ("과거낙찰", lambda: step_backfill(api, meta, store, now, save_all, horizon)),
        ("지역보강", lambda: step_region_fill(api, meta, store, now, save_all)),
        ("상세", lambda: step_details(api, meta, store, ostore, regions, now, save_all)),
        ("과거낙찰", lambda: step_backfill(api, meta, store, now, save_all)),
    ]
    only = {s.strip() for s in (os.environ.get("STEPS") or "").split(",") if s.strip()}
    fatal = None
    for name, fn in steps:
        if only and name not in only:
            continue
        try:
            fn()
        except BudgetExhausted as e:
            log(f"[{name}] 한도/시간 소진: {e}")
            errors.append(f"{name}: 한도 소진 ({e})")
        except FatalApiError as e:
            log(f"[{name}] 치명적 오류: {e}")
            errors.append(f"{name}: {e}")
            fatal = e
            break
        except Exception as e:  # 한 단계가 죽어도 나머지는 진행
            traceback.print_exc()
            errors.append(f"{name}: {type(e).__name__}: {e}")
    save_all(final=True)
    if fatal:
        sys.exit(2)


if __name__ == "__main__":
    main()

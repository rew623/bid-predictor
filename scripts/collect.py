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
  물품최근   물품 최근 낙찰 + 물품 기초금액(최근 60일)   → data/thng/{시도}.json
  물품과거   물품 과거 낙찰을 한 달씩 과거로 24개월까지 (진행: meta.thng_backfill), 낙찰정보 THNG_RESERVE·입찰공고 REGION_RESERVE 회는 남김
  빈달       이미 지난 달 중 빈 달 다시 받기 (meta.refill, 기본 2026-03~06)
  실제 순서: 공고 → 최근낙찰 → 상세 → 빈달 → 물품최근 → 물품과거 → 지역보강 → 과거낙찰(24개월) → 상세 → 과거낙찰(나머지)

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
from thng_curve import ThngCurve, bucket as curve_bucket, lottery_curve, real_window  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
KST = dt.timezone(dt.timedelta(hours=9))

DAILY_LIMIT = int(os.environ.get("API_DAILY_LIMIT") or 950)
MAX_MINUTES = float(os.environ.get("MAX_MINUTES") or 300)
MIN_GAP = float(os.environ.get("MIN_GAP") or 0.25)   # 호출 사이 최소 간격(초) — 운영계정 초당 한도를 앱과 나눠 쓰기 위해
MAX_FILE_BYTES = 45 * 1024 * 1024          # 파일 하나 50MB 미만 유지
ROWS = 999                                  # 페이지당 건수
NOTICE_CACHE_DAYS = 60                      # 낙찰 정보 보강용 공고 보관 기간
RECENT_SCSBID_DAYS = 40                     # 매일 다시 훑는 최근 낙찰 기간
BACKFILL_YEARS = 3
DETAIL_RESERVE = max(250, int(DAILY_LIMIT * 0.4))   # 첫 상세 단계는 낙찰정보 호출을 이만큼 남겨 물품·과거 수집에 쓴다 (운영계정 9500 → 3800)
RECENT_FIRST_MONTHS = 24                    # 과거 낙찰은 이 기간을 먼저 채운 뒤 상세 → 나머지 과거 순으로
DETAIL_MAX_TRIES = 3
NET_FAIL_STOP = 3                           # 조달청 접속이 연속 이만큼 안 되면 그 실행은 멈춘다 (2026-09-26 새벽 5시간 헛돈 일)
OPEN_TOP = 30                               # 관심 시·도 밖 개찰 상세: 금액 행으로 남길 상위 업체 수
WATCH_BIZ = {re.sub(r"\D", "", b) for b in (os.environ.get("WATCH_BIZ") or "").split(",") if re.sub(r"\D", "", b)}   # 저장소 변수 — 순위 밖이어도 행을 남길 업체(우리 업체)
try:   # 관심 시·도(전원 행 그대로 저장) = detail_priority.json 의 sido
    FULL_SIDOS = set(json.load(open(ROOT / "scripts" / "detail_priority.json", encoding="utf-8")).get("sido") or ["강원"])
except (OSError, ValueError):
    FULL_SIDOS = {"강원"}
REGION_RESERVE = 250                        # 지역보강은 입찰공고 호출을 이만큼 남긴다 (뒤의 과거 수집 몫)
THNG_RESERVE = 250                          # 물품과거는 낙찰정보 호출을 이만큼 남긴다 (뒤의 개찰 상세 몫)
THNG_BID_RESERVE = 250                      # 물품과거는 입찰공고 호출을 이만큼 남긴다 (뒤의 지역보강·공사 과거 수집 몫)
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
    "thng_prepar": ("scsbid", "getOpengResultListInfoThngPreparPcDetail", "2"),  # 물품 복수예비가격 상세 (물품곡선)
    "notice_list": ("bid", "getBidPblancListInfoCnstwk", "1"),           # 공사 입찰공고 목록
    "notice_bsis": ("bid", "getBidPblancListInfoCnstwkBsisAmount", "1"),  # 기초금액·A값
    "notice_license": ("bid", "getBidPblancListInfoLicenseLimit", "1"),   # 면허제한
    "notice_region": ("bid", "getBidPblancListInfoPrtcptPsblRgn", "1"),   # 참가가능지역
    "thng_list": ("scsbid", "getScsbidListSttusThng", "1"),               # 물품 낙찰 목록
    "thng_bsis": ("bid", "getBidPblancListInfoThngBsisAmount", "1"),      # 물품 기초금액·예가범위
    "thng_notice": ("bid", "getBidPblancListInfoThng", "1"),              # 물품 공고 (낙찰하한율·계약방법 — 낙찰 목록엔 없음)
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
F_MFRC = ["indstrytyMfrcFldList"]
F_LMT_GRP = ["lmtGrpNo"]            # 면허제한 그룹 번호   # 면허제한의 주력분야 (예: 금속구조물ㆍ창호ㆍ온실공사) — 대업종 안에서 더 좁힌 제한
F_RGN = ["prtcptPsblRgnNm", "rgnNm"]
F_AMT = ["sucsfbidAmt", "scsbdAmt"]
F_RATE = ["sucsfbidRate", "scsbdRate"]
F_CNT = ["prtcptCnum", "prtcptCnt"]
F_WIN = ["bidwinnrNm", "scsbdrNm"]
F_WIN_BIZ = ["bidwinnrBizno", "scsbdrBizno"]
F_WIN_CEO = ["bidwinnrCeoNm"]
F_WIN_ADR = ["bidwinnrAdrs"]
F_WIN_TEL = ["bidwinnrTelNo"]


class CorpInfo:
    """낙찰 업체 대표자·주소·전화 → data/corp_info.json {사업자번호: [대표자, 주소, 전화]} (업체 검색용, 낙찰 목록에만 있는 정보라 낙찰 기록마다 반복 저장하지 않고 여기 한 곳에)"""
    path = DATA / "corp_info.json"

    def __init__(self):
        self.d = load_json(self.path, {})
        self.dirty = False

    def add(self, it):
        biz = re.sub(r"\D", "", str(pick(it, F_WIN_BIZ) or ""))
        if not biz:
            return
        v = [(pick(it, k) or "").strip() for k in (F_WIN_CEO, F_WIN_ADR, F_WIN_TEL)]
        if any(v) and self.d.get(biz) != v:
            self.d[biz] = v
            self.dirty = True

    def save(self):
        if self.dirty:
            write_if_changed(self.path, dumps(self.d))
            self.dirty = False


CORP_INFO = None


def corp_info():
    global CORP_INFO
    if CORP_INFO is None:
        CORP_INFO = CorpInfo()
    return CORP_INFO
F_CEO = ["prcbdrCeoNm"]
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


def hide_key(s):
    """오류 문구 속 요청 주소의 serviceKey 를 가린다 — Actions 로그·meta.json(공개 저장소)에 키가 찍히던 일(2026-09-27)"""
    return re.sub(r"(?i)(serviceKey=)[^&\s'\"]+", r"\1***", str(s))


def log(*a):
    print(now_kst().strftime("%H:%M:%S"), *(hide_key(x) for x in a), flush=True)


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
        msg = hide_key(msg)
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
        self.cooldowns = 0    # 접속 실패로 10분 쉰 횟수
        self.net_fail = 0     # 연속 접속 실패 수 — NET_FAIL_STOP 번이면 오늘은 조달청이 안 되는 것으로 보고 멈춘다
        self.last_at = 0.0    # 마지막 호출 시각 (MIN_GAP 간격 유지)
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
        if self.net_fail >= NET_FAIL_STOP:
            raise BudgetExhausted("network")
        bases = SERVICES[svc]
        known = self.bases.get(svc)
        if known in bases:
            bases = [known] + [b for b in bases if b != known]
        last_err = None
        reached = False
        for base in bases:
            url = f"{base}/{opname}"
            q = {"serviceKey": self.key, "type": "json", **params}
            resp = None
            for attempt in range(8):
                gap = MIN_GAP - (time.time() - self.last_at)
                if gap > 0:
                    time.sleep(gap)   # 같은 서비스키를 앱(브라우저)도 쓰므로 초당 호출을 남겨 둔다
                self.last_at = time.time()
                try:
                    resp = self.session.get(url, params=q, timeout=(15, 60))   # 접속 15초, 응답 60초
                except requests.RequestException as e:
                    last_err = e
                    resp = None
                    time.sleep(2 ** min(attempt, 3))
                    continue
                if "PER_SECOND_EXCEEDS" in (resp.text or "") or "초당 서비스" in (resp.text or ""):
                    # 초당 한도(코드 23)는 빈 결과가 아니다 — 쉬었다 다시 (2026-09-27: 빈 응답으로 처리될 뻔함)
                    last_err = "초당 요청 제한"
                    resp = None
                    time.sleep(1 + attempt)
                    continue
                break
            if resp is None:
                continue
            reached = True
            self.net_fail = 0
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
        if not reached:
            self.net_fail += 1
            if self.net_fail >= NET_FAIL_STOP and self.cooldowns < 2 and self.time_left() > 25:
                # 2026-09-27 14·20시: Actions → apis.data.go.kr 접속 시간 초과가 수십 분 이어짐 → 바로 멈추지 말고 10분 쉬고 다시 (실행마다 2번까지)
                self.cooldowns += 1
                log(f"  ! 조달청 접속 {NET_FAIL_STOP}번 연속 실패 — 10분 쉬고 다시 ({self.cooldowns}/2)")
                time.sleep(600)
                self.net_fail = 0
            if self.net_fail >= NET_FAIL_STOP:
                log(f"  ! 조달청 접속이 {NET_FAIL_STOP}번 연속 안 됨 — 오늘 수집은 여기서 멈춤")
                raise BudgetExhausted("network")
        raise ApiError("NET", f"{opname}: {last_err}")

    @staticmethod
    def _parse(j, op):
        if isinstance(j, dict) and "OpenAPI_ServiceResponse" in j:   # 게이트웨이 오류(키·한도) — 빈 결과로 보면 안 됨
            h = (j["OpenAPI_ServiceResponse"] or {}).get("cmmMsgHeader") or {}
            raise ApiError(str(h.get("returnReasonCode", "99")), h.get("returnAuthMsg") or h.get("errMsg") or "")
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
        docs = notice_docs(it)
        if docs:
            e["docs"] = docs
        nego = is_nego(it)
        if nego:
            e["nego"] = nego
        else:
            e.pop("nego", None)
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
        mf = str(pick(it, F_MFRC) or "").strip()
        if mf:
            lst = e.setdefault("mf_list", [])
            if mf not in lst:
                lst.append(mf)
        if raw:   # 면허제한 한 줄: [그룹번호, 면허 원문, 주력분야 원문] — 같은 그룹은 모두 필요, 그룹끼리는 택일
            row = [int(num(pick(it, F_LMT_GRP)) or 1), raw, mf]
            lst = e.setdefault("lim", [])
            if row not in lst:
                lst.append(row)

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
            if not e.get("nm") or e.get("cancel") or e.get("old") or e.get("nego"):
                continue
            close = e.get("close")
            if close and close < cur:
                continue
            if not close and (e.get("ntce") or "") < recent:
                continue
            out.append(clean({k: e.get(k) for k in (
                "id", "no", "ord", "nm", "org", "dmd", "sido", "sgg", "lic", "rgn", "base", "est",
                "floor", "a", "net", "rng", "ntce", "close", "open", "url", "seen", "sd")}))
        out.sort(key=lambda x: (x.get("close") or "9999", x["id"]))
        return out


def other_quals(e):
    """건설 면허 23개로 못 바꾼 참가자격(예: 국유림영림단, 산림사업법인) 원문 — 우리 면허와 안 겹치므로 '참가 불가'로 판정되게 남긴다"""
    raw = e.get("lic_list") or ([e["lic_raw"]] if e.get("lic_raw") else [])
    return list(dict.fromkeys(t.split("/")[0].strip() for t in raw if t and t.split("/")[0].strip()))


def write_lic_map(cache):
    """앱 실시간 검색용: 최근 60일 공사 공고별 면허제한·참가가능지역 → data/lic_map.json
    {"v":1, "lic":[면허명…], "items":{공고ID:[면허 번호…]}, "rg":[지역 원문…], "rgn":{공고ID:[지역 번호…]}, "mfn":[주력분야 원문…], "mf":{공고ID:[번호…]},
     "grp":{공고ID:[[그룹번호, lic 번호, mfn 번호 또는 -1]…]}}
    (조달청 검색조건의 업종 필터가 0건을 돌려주는 문제 대신 + 실시간 공고엔 면허제한·참가가능지역이 없어 '참가 가능' 판정에 씀)"""
    names, index, items = [], {}, {}
    rnames, rindex, rgns = [], {}, {}
    mnames, mindex, mfs = [], {}, {}
    grps = {}

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
        lic = e.get("lic") or other_quals(e)
        if lic:
            items[id_] = ids(lic, names, index)
        if e.get("rgn"):
            rgns[id_] = ids(e["rgn"], rnames, rindex)
        if e.get("mf_list"):
            mfs[id_] = ids(e["mf_list"], mnames, mindex)
        if e.get("lim"):
            rows = []
            for g, raw, mf in e["lim"]:
                lic = (normalize_licenses(raw) or other_quals({"lic_raw": raw}) or [raw])[0]
                rows.append([g, ids([lic], names, index)[0], ids([mf], mnames, mindex)[0] if mf else -1])
            grps[id_] = rows
    return write_if_changed(DATA / "lic_map.json", dumps({"v": SCHEMA_VERSION, "lic": names, "items": items, "rg": rnames, "rgn": rgns,
                                                          "mfn": mnames, "mf": mfs, "grp": grps}))


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
                 "rate", "cnt", "floor", "a", "net", "rng", "rgn", "cm", "date", "win", "winBiz", "sr")


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
        corp_info().add(it)
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


class ThngStore(ScsbidStore):
    """물품 낙찰 → data/thng/{시도}.json (항목 형식은 scsbid 와 같음, A값·면허·순공사원가 없음)"""
    dir = DATA / "thng"


class OldStore(ScsbidStore):
    """3년보다 옛 낙찰 — 관심 시·도(강원) 것만 data/scsbid_old/{시도}.json (2026-09-28 요청: 업체 낙찰 이력을 길게).
    전국을 다 두면 저장소가 커져서 강원만. 앱은 이 파일을 안 읽고 corp_index.py(업체 낙찰 이력·수)만 읽는다.
    공고 보강(기초금액·A값)은 안 함 — 낙찰자·금액·공고명만 필요. 물품은 kind='물품'으로 같이."""
    dir = DATA / "scsbid_old"

    def merge(self, it, cache, kind=None):
        super().merge(it, cache)
        id_ = notice_id(it)[0]
        r = self.recs.get(id_)
        if r is not None and r.get("sido") not in FULL_SIDOS:
            del self.recs[id_]
            ids = self.by_no.get(r["no"], [])
            if id_ in ids:
                ids.remove(id_)
        elif r is not None and kind:
            r["cm"] = kind   # 공사/물품 구분(옛 파일에서는 cm = 업무)

    def save(self):
        files, counts = {}, {}
        groups = {}
        for r in self.recs.values():
            groups.setdefault(r.get("sido") or "기타", []).append(r)
        for sido, recs in groups.items():
            recs.sort(key=lambda r: (r.get("date") or "", r["id"]), reverse=True)
            items = [clean({k: r.get(k) for k in ("id", "no", "ord", "nm", "org", "dmd", "sido", "sgg", "amt", "rate", "cnt", "win", "winBiz", "date", "base", "plan", "cm")}) for r in recs]
            files[sido] = split_write(self.dir / sido, {"sido": sido, "v": SCHEMA_VERSION}, items)
            counts[sido] = len(items)
        return files, counts


OLD_YEARS = int(os.environ.get("OLD_YEARS") or 6)   # 옛 낙찰(강원만)을 몇 년 전까지


def step_old_backfill(api, meta, old, now, checkpoint):
    """과거낙찰(3년)이 끝난 뒤 그보다 옛 달을 한 달씩 — 공사·물품 낙찰 목록만, 강원 것만 저장. 진행 meta.backfill_old {cursor, target, done}"""
    if not meta.get("backfill", {}).get("done"):
        return
    bo = meta.setdefault("backfill_old", {})
    bo.setdefault("target", (now - dt.timedelta(days=365 * OLD_YEARS)).strftime("%Y%m%d"))
    if bo.get("done"):
        return
    target = dt.datetime.strptime(bo["target"], "%Y%m%d").replace(tzinfo=KST)
    start = dt.datetime.strptime(meta["backfill"]["target_start"], "%Y%m%d").replace(tzinfo=KST) - dt.timedelta(days=1)
    cursor = bo.get("cursor") or start.strftime("%Y%m%d")
    while True:
        cur = dt.datetime.strptime(cursor, "%Y%m%d").replace(tzinfo=KST)
        if cur < target:
            bo["done"] = True
            log("[옛낙찰] 완료")
            return
        if api.remaining("scsbid") < 40 or api.time_left() < 15:
            log(f"[옛낙찰] 여기까지, 커서 {cursor}")
            return
        end = cur.replace(hour=23, minute=59)
        bgn = max(cur.replace(day=1, hour=0, minute=0), target)
        n0 = len(old.recs)
        for op, kind in (("scsbid_list", "공사"), ("thng_list", "물품")):
            for it in api.fetch_range(op, bgn, end):
                old.merge(it, None, kind)
        old.dirty = True
        cursor = bo["cursor"] = (bgn - dt.timedelta(days=1)).strftime("%Y%m%d")
        log(f"[옛낙찰] {bgn:%Y-%m} 강원 {len(old.recs) - n0}건")
        checkpoint()


def enrich_thng(store, it):
    """물품 기초금액 API 한 행 → 같은 공고의 낙찰 레코드에 기초금액·예가범위"""
    _, no, ord_ = notice_id(it)
    if no not in store.by_no:
        return
    n = clean({"no": no, "ord": ord_, "base": to_int(pick(it, F_BASE)), "rng": price_range(it)})
    if n.get("base"):
        store.enrich_by_notice(n)


F_CNTRCT = ["cntrctCnclsMthdNm"]


def enrich_thng_notice(store, it):
    """물품 공고 한 행 → 같은 공고의 낙찰 레코드에 낙찰하한율(floor)·계약방법(cm, 예: 수의계약·제한경쟁)"""
    _, no, ord_ = notice_id(it)
    ids = store.by_no.get(no, [])
    vals = clean({"floor": to_rate(pick(it, F_FLOOR), 3), "cm": (pick(it, F_CNTRCT) or "").strip() or None})
    for id_ in ids if vals else []:
        r = store.recs[id_]
        if r.get("ord") == ord_ or len(ids) == 1:
            for k, v in vals.items():
                if r.get(k) != v:
                    r[k] = v
                    store.dirty = True


# ---------------------------------------------------------------- 개찰 상세 (전체 순위 + 복수예가)
class OpeningStore:
    """data/opening/{시도}/{연도}.json

    파일 형식: {"sido","year","corps":[[업체명,사업자번호,대표자?],...],
               "bids":{공고ID:{"date","plan","base","p":[[번호,가격,추첨(0/1),추첨횟수],...],
                              "r":[[순위,업체idx,투찰금액,투찰률,비고?],...]}}}
    index.json: {"done":{공고ID:연도}, "fail":{공고ID:시도횟수}}
    """
    dir = DATA / "opening"
    top_only = False   # True(물품): 상위 30곳 + 우리 업체 행만, 전원 c·x 는 안 남김
    force_compact = False   # True(옛 강원): 관심 시·도여도 상위 30곳 금액 + 전원 c·x (OldOpeningStore)

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
                    if b.get("c"):
                        b["c"] = [self._corp(y, *corps[ci]) for ci in b["c"]]
                    y["bids"][bid_id] = b
            self.years[key] = y
        return self.years[key]

    @staticmethod
    def _corp(y, name, biz, ceo=None):
        k = biz or name
        if k not in y["cidx"]:
            y["cidx"][k] = len(y["corps"])
            y["corps"].append([name, biz] + ([ceo] if ceo else []))
        elif ceo and len(y["corps"][y["cidx"][k]]) < 3:
            y["corps"][y["cidx"][k]].append(ceo)
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
            row = [rank if rank is not None else 0, self._corp(y, name, biz, (pick(it, F_CEO) or "").strip() or None), amt, to_rate(pick(it, F_BID_RATE), 4)]
            note = (pick(it, F_NOTE) or "").strip()
            if note:
                row.append(note[:30])
            rows.append(row)
        rows.sort(key=lambda r: (r[0] <= 0, r[0], r[2]))
        compact = {}
        if self.top_only:   # 물품은 참가 수천 곳 — 1위 자리(적격 탈락 포함 순위)만 알면 역검증이 되므로 상위 30곳만
            compact = {"k": sum(1 for r in rows if r[0] > 0), "n": len(rows)}
            if any(y["corps"][r[1]][1] in WATCH_BIZ for r in rows):   # 우리가 넣은 공고는 전원 투찰률(순위 순, 앞 값과의 차이 ×1000)까지 — 우리 투찰 상세 분석용(2026-09-28)
                xs = [round((r[3] or 0) * 1000) for r in rows]
                compact["x"] = xs[:1] + [b - a for a, b in zip(xs, xs[1:])]
            rows = rows[:OPEN_TOP] + [r for r in rows[OPEN_TOP:] if y["corps"][r[1]][1] in WATCH_BIZ]
        elif self.force_compact or sido not in FULL_SIDOS:   # 전국 전원 행은 1년 약 1GB → 상위 30곳(+우리 업체) 금액 행 + 전원은 업체·투찰률(국방 v2 와 같은 방식)
            compact = {"c": [r[1] for r in rows], "k": sum(1 for r in rows if r[0] > 0)}
            xs = [round((r[3] or 0) * 1000) for r in rows]
            compact["x"] = xs[:1] + [b - a for a, b in zip(xs, xs[1:])]
            rows = rows[:OPEN_TOP] + [r for r in rows[OPEN_TOP:] if y["corps"][r[1]][1] in WATCH_BIZ]
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
                                      "base": base or rec.get("base"), "p": p, "r": rows, **compact})
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
                        c = y["corps"][row[1]]
                        k = c[1] or c[0]
                        if k not in cidx:
                            cidx[k] = len(corps)
                            corps.append(list(c[:3]))
                        rows.append([row[0], cidx[k]] + row[2:])
                    b["r"] = rows
                    if b.get("c"):
                        cc = []
                        for ci in b["c"]:
                            c = y["corps"][ci]
                            k = c[1] or c[0]
                            if k not in cidx:
                                cidx[k] = len(corps)
                                corps.append(list(c[:3]))
                            cc.append(cidx[k])
                        b["c"] = cc
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


class OldOpeningStore(OpeningStore):
    """3년보다 옛 강원 공사 개찰 상세 → data/opening_old/{시도}/{연도}.json (2026-09-28 요청 '업체 검색하면 과거 강원 자료 다').
    전원 행을 두면 3년에 약 150MB 라 강원 밖 시·도처럼 압축(상위 30곳 금액 + WATCH_BIZ + 전원 업체 c·투찰률 x·k + 복수예가 p) — 약 1/3.
    앱은 업체 보기에서만 읽는다(평소 화면은 안 읽음)."""
    dir = DATA / "opening_old"
    force_compact = True

    def add(self, sido, rec, ranks, prices):
        out = super().add(sido, rec, ranks, prices)
        b = self.year(sido, rec["date"][:4])["bids"].get(rec["id"])
        if b is not None:   # 앱이 옛 낙찰 파일을 안 읽으므로 공고명·기관·참가 수를 같이
            b.update(clean({"nm": (rec.get("nm") or "")[:60] or None, "org": rec.get("dmd") or rec.get("org"), "n": len(ranks)}))
        return out


def step_old_details(api, meta, old, oostore, now, checkpoint, minutes):
    """옛낙찰(강원 공사)마다 개찰 순위 + 복수예가 — 최신(3년 전 바로 앞)부터 과거로. 진행은 oostore index(done/fail)."""
    stop = time.time() + minutes * 60
    queue = []
    for r in old.recs.values():
        if (r.get("cm") or "공사") != "공사" or r.get("sido") not in FULL_SIDOS or not r.get("date"):
            continue
        ix = oostore.idx(r["sido"])
        if r["id"] in ix["done"] or ix["fail"].get(r["id"], 0) >= DETAIL_MAX_TRIES:
            continue
        queue.append(r)
    queue.sort(key=lambda r: r["date"], reverse=True)
    log(f"[옛상세] 대기 {len(queue)}건")
    n = 0
    for r in queue:
        if time.time() > stop or api.remaining("scsbid") < 50 or api.time_left() < 10:
            break
        cut = r["id"].rfind("-")
        rec = dict(r, no=r.get("no") or r["id"][:cut], ord=r.get("ord") or r["id"][cut + 1:])
        try:
            ranks = list(api.paged("opening_rank", {"bidNtceNo": rec["no"], "bidNtceOrd": rec["ord"]}))
            prices = list(api.paged("prepar_detail", {"inqryDiv": "2", "bidNtceNo": rec["no"]}))
        except ApiError as e:
            log(f"  ! {rec['id']} {e}")
            oostore.fail(rec["sido"], rec["id"])
            continue
        for lst in (ranks, prices):
            rb = [to_int(pick(i, F_RBID)) or 0 for i in lst]
            if rb and max(rb) > 0:
                lst[:] = [i for i, k in zip(lst, rb) if k == max(rb)]
        prices = [p for p in prices if str(pick(p, F_ORD) or rec["ord"]) == rec["ord"]] or prices
        if not ranks:
            oostore.fail(rec["sido"], rec["id"])
            continue
        oostore.add(rec["sido"], rec, ranks, prices)
        n += 1
        if n % 200 == 0:
            checkpoint()
    log(f"  이번 실행 {n}건")


class ThngOpeningStore(OpeningStore):
    """물품 개찰 순위 → data/opening_thng/{시도}/{연도}.json (2026-09-27, 물품 역검증용 — 낙찰 목록만으로는 1위가 하한 바로 위가 아닌
    공고(36%, 적격·규격 탈락 추정)에서 '우리가 x 로 넣었으면'을 판정할 수 없다). 형식은 opening 과 같고 r 은 상위 30곳 + 우리 업체,
    k = 순위 있는 업체 수, n = 전체 행 수. 복수예가(p)는 안 받음(예정가격은 낙찰 기록 plan)."""
    dir = DATA / "opening_thng"
    top_only = True


# ---------------------------------------------------------------- 단계별 작업
def step_notices(api, meta, cache, now):
    last = parse_dt((meta.get("notice_last") or "")[:16].replace("T", " "))
    days = 30 if not cache.items or not last else min(30, max(2, (now - last).days + 2))
    lic_days = days if meta.get("lim_scan") else 30   # 그룹·주력분야(lim)를 처음 받을 때 한 번은 30일치 면허제한을 다시
    bgn = now - dt.timedelta(days=days)
    seen = now.isoformat(timespec="minutes")
    log(f"[공고] 최근 {days}일")
    list_bgn = bgn if meta.get("doc_scan") and meta.get("nego_scan2") else now - dt.timedelta(days=30)   # 공고문 첨부(docs)·수의시담 등(nego)을 처음 받을 때 한 번은 30일치 목록을 다시
    for it in api.fetch_range("notice_list", list_bgn, now):
        cache.add_notice(it, seen)
    meta["doc_scan"] = True
    meta["nego_scan2"] = True
    for it in api.fetch_range("notice_bsis", now - dt.timedelta(days=max(days, 14)), now):
        cache.add_bsis(it)
    for it in api.fetch_range("notice_license", now - dt.timedelta(days=lic_days), now):
        cache.add_license(it)
    meta["lim_scan"] = True
    for it in api.fetch_range("notice_region", bgn, now):
        cache.add_region(it)
    cache.finalize(now)
    meta["notice_last"] = seen


NEGO_KINDS = [("시담", "수의시담"), ("협상", "협상에 의한 계약"), ("2단계", "2단계 경쟁"), ("규격가격동시", "규격가격 동시입찰"),
              ("제안", "제안서 평가"), ("종합평가", "종합평가"), ("종합낙찰", "종합낙찰")]   # app.js NEGO_KINDS 와 같게


def is_nego(it):
    """'바로 금액만 투찰해서 계약'이 아닌 공고 — 수의시담(정해진 계약 대상자만, 나라장터 '정보공개 차원에서 공고')·협상에 의한 계약·2단계 경쟁·
    규격가격 동시입찰(제안·규격 심사)·제안서·종합평가 → 우리 공고·진행중 목록에서 뺀다(2026-09-29 사용자). 해당하면 이유 이름, 아니면 ''"""
    txt = " ".join(str(it.get(k) or "") for k in ("bidMethdNm", "sucsfbidMthdNm", "sucsfbidMthdAppStd", "cntrctCnclsMthdNm"))
    for kw, name in NEGO_KINDS:
        if kw in txt:
            return name
    return ""


def notice_docs(it):
    """공고문 첨부 후보 최대 2개 [[파일명, URL]] — 이름에 '공고'가 든 것, hwp·hwpx 먼저(글자 뽑기가 확실), 없으면 첫 파일"""
    files = [((it.get(f"ntceSpecFileNm{i}") or "").strip(), (it.get(f"ntceSpecDocUrl{i}") or "").strip()) for i in range(1, 11)]
    files = [f for f in files if f[0] and f[1]]
    cand = [f for f in files if "공고" in f[0]] or files[:1]
    cand.sort(key=lambda f: 0 if f[0].lower().endswith((".hwp", ".hwpx")) else 1)
    return [list(f) for f in cand[:2]]


DOC_TRIES = 3


def step_doc_flags(api, cache, now, minutes):
    """진행중 공사 공고의 공고문 첨부를 받아 API 에 없는 표시를 찾는다 — 지금은 사실조사(사전단속) sd (notice_doc.FLAGS).
    공고마다 한 번(sdc=1), 실패는 DOC_TRIES 번까지. 새 공고만 보므로 평소엔 몇 분."""
    import notice_doc
    t_end = time.time() + 60 * max(0, min(minutes, api.time_left() - 5))
    cur = now.strftime("%Y-%m-%d %H:%M")
    todo = [e for e in cache.items.values() if e.get("docs") and not e.get("nego") and not e.get("sdc") and e.get("sdt", 0) < DOC_TRIES
            and not e.get("cancel") and not e.get("old") and (e.get("close") or "9999") >= cur]
    todo.sort(key=lambda e: (e.get("sido") not in FULL_SIDOS, e.get("sdt", 0), e.get("close") or "9999"))   # 관심 시·도(강원) → 처음 보는 공고 → 마감 임박
    done = hit = fail = 0
    for e in todo:
        if time.time() > t_end:
            break
        text = ""
        for nm, url in e["docs"]:   # hwp 가 안 읽히면 pdf 변환본으로
            try:
                r = requests.get(url, timeout=40, headers={"User-Agent": "Mozilla/5.0 (bid-predictor collector)"})
                r.raise_for_status()
                time.sleep(0.3)
                if len(r.content) > 30_000_000:
                    continue
                text = notice_doc.text_of(r.content, nm)
            except Exception as ex:  # noqa: BLE001 — 한 건 실패는 다음 실행에
                log("  공고문 실패", e.get("id"), nm, str(ex)[:120])
            if text.strip():
                break
        if text.strip():
            e.update(notice_doc.flags_of(text))
            e["sdc"] = 1
            e.pop("sdt", None)
            done += 1
            hit += bool(e.get("sd"))
        else:
            e["sdt"] = e.get("sdt", 0) + 1
            fail += 1
    log(f"[공고문] 확인 {done}건(사전단속 {hit}) · 실패 {fail} · 남음 {max(0, len(todo) - done - fail)}")


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
        cnt, nn = backfill_month(api, store, bgn, end)
        cursor = bf["cursor"] = (bgn - dt.timedelta(days=1)).strftime("%Y%m%d")
        bf["months_done"] = bf.get("months_done", 0) + 1
        bf["oldest"] = bgn.strftime("%Y%m%d")
        log(f"  낙찰 {cnt}건, 공고 보강 {nn}건")
        checkpoint()


def backfill_month(api, store, bgn, end):
    """한 기간의 공사 낙찰 목록 + 같은 기간 공고로 기초금액·A값·면허 보강. (낙찰 건수, 보강 공고 수)"""
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
    for e in month.items.values():
        finish_notice(e)
        store.enrich_by_notice(e)
    return cnt, len(month.items)


REFILL_DEFAULT = ["202606", "202605", "202604", "202603"]   # 2026-09 확인: 이 달들만 낙찰 수가 평소의 1/10 이하 (수집 중 오류로 빈 것)


def step_refill(api, meta, store, now, checkpoint):
    """이미 지나간 달 중 비어 있는 달을 다시 받는다. meta.refill = [YYYYMM…] (처리하면 목록에서 뺀다)"""
    todo = meta.setdefault("refill", list(REFILL_DEFAULT))
    while todo:
        if api.remaining("scsbid") < 60 or api.remaining("bid") < REGION_RESERVE or api.time_left() < 20:
            log(f"[빈달] 오늘 몫 끝, 남은 달 {todo}")
            return
        m = todo[0]
        bgn = dt.datetime.strptime(m + "01", "%Y%m%d").replace(tzinfo=KST)
        end = (bgn + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(minutes=1)
        log(f"[빈달] {bgn:%Y-%m}")
        cnt, nn = backfill_month(api, store, bgn, end)
        log(f"  낙찰 {cnt}건, 공고 보강 {nn}건")
        todo.pop(0)
        checkpoint()


INFO_MONTHS = 36          # 업체정보(대표자·주소)를 채울 과거 기간
INFO_PER_RUN = 12         # 한 번에 채울 달 수 (공사+물품 낙찰 목록, 한 달 약 25회 호출) — 3번(1.5일)이면 36개월


def step_corp_info(api, meta, now, checkpoint):
    """과거 낙찰 목록을 다시 훑어 낙찰 업체의 대표자·주소·전화만 corp_info.json 에 채운다(낙찰 기록은 그대로).
    진행: meta.info_fill {cursor: YYYYMM, done}. 새 낙찰은 최근낙찰·물품최근이 자동으로 채운다."""
    st = meta.setdefault("info_fill", {})
    if st.get("done"):
        return
    cur = dt.datetime.strptime(st.get("cursor") or now.strftime("%Y%m"), "%Y%m").replace(tzinfo=KST)
    stop = (now.replace(day=1) - dt.timedelta(days=31 * INFO_MONTHS)).strftime("%Y%m")
    for _ in range(INFO_PER_RUN):
        bgn = cur.replace(day=1)
        end = (bgn + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(minutes=1)
        log(f"[업체정보] {bgn:%Y-%m}")
        for op in ("scsbid_list", "thng_list"):
            for it in api.fetch_range(op, bgn, min(end, now)):
                corp_info().add(it)
        cur = bgn - dt.timedelta(days=1)
        st["cursor"] = cur.strftime("%Y%m")
        if st["cursor"] < stop:
            st["done"] = True
            break
        checkpoint()
    log(f"  업체정보 {len(corp_info().d)}곳")


def step_thng_recent(api, meta, tstore, now, cache=None):
    """물품 최근 낙찰 + 최근 60일 물품 기초금액으로 보강. 같은 응답으로 진행중 물품 공고(data/goods.json)도 만든다 (추가 호출 없음)"""
    log(f"[물품] 최근 {RECENT_SCSBID_DAYS}일")
    n = 0
    for it in api.fetch_range("thng_list", now - dt.timedelta(days=RECENT_SCSBID_DAYS), now):
        tstore.merge(it, None)
        n += 1
    notices = api.fetch_range("thng_notice", now - dt.timedelta(days=NOTICE_CACHE_DAYS), now)
    for it in notices:
        enrich_thng_notice(tstore, it)
    bsis = api.fetch_range("thng_bsis", now - dt.timedelta(days=NOTICE_CACHE_DAYS), now)
    for it in bsis:
        enrich_thng(tstore, it)
    log(f"  물품 낙찰 {n}건 조회")
    write_goods(notices, bsis, cache, now)


def write_goods(notices, bsis, cache, now):
    """진행중 물품 공고 → data/goods.json {"v":1, "updated_at", "items":[…]} (마감 임박 순)
    항목: id, no, ord, nm, org, dmd, sido, sgg, rgn(참가가능지역), inds(업종 제한 원문), base, est, floor, rng, cm(계약방법),
          mnf(제조 = 직접생산 필요 1), plim(물품분류 제한 1), prd(세부품명), ntce, close, open, url
    참가가능지역·업종제한은 '공고' 단계가 모든 업무(공사·물품·용역)를 받아 notice_cache 에 둔 것을 쓴다."""
    cur = now.strftime("%Y-%m-%d %H:%M")
    base = {}
    for it in bsis:
        id_, _, _ = notice_id(it)
        if id_:
            base[id_] = clean({"base": to_int(pick(it, F_BASE)), "rng": price_range(it)})
    latest, out = {}, {}
    for it in notices:
        id_, no, ord_ = notice_id(it)
        if not id_:
            continue
        if ord_ < latest.get(no, ""):
            continue
        latest[no] = ord_
        close = norm_dt(pick(it, F_CLOSE_DT))
        if "취소" in (pick(it, F_KIND) or "") or not close or close < cur or is_nego(it):
            out.pop(no, None)
            continue
        e = (cache.items.get(id_) if cache else None) or {}
        rgn = e.get("rgn") or []
        dmd, org = pick(it, F_DMND_ORG), pick(it, F_NTCE_ORG)
        sido, sgg = parse_region(None, rgn[0] if len(rgn) == 1 else None, dmd, org)
        inds = list(dict.fromkeys(t.split("/")[0].strip() for t in (e.get("lic_list") or []) if t.strip()))
        out[no] = clean({
            "id": id_, "no": no, "ord": ord_, "nm": (pick(it, F_NAME) or "").strip(), "org": org, "dmd": dmd,
            "sido": sido, "sgg": sgg, "rgn": rgn, "inds": inds,
            "est": to_int(pick(it, F_EST)), "floor": to_rate(pick(it, F_FLOOR), 3), "cm": (pick(it, F_CNTRCT) or "").strip() or None,
            "mnf": 1 if it.get("mnfctYn") == "Y" else None, "plim": 1 if it.get("prdctClsfcLmtYn") == "Y" else None,
            "prd": (it.get("dtilPrdctClsfcNoNm") or "").strip() or None,
            "bdg": to_int(it.get("asignBdgtAmt")), "qty": to_int(it.get("prdctQty")), "unp": to_int(it.get("prdctUprc")),
            "np": len(re.findall(r"\[", it.get("purchsObjPrdctList") or "")) or None,   # 기초금액이 안 올 때 추정 근거(배정예산·수량·단가·품목 수) — 아직 검증 중, 앱은 안 씀
            "ntce": norm_dt(pick(it, F_NTCE_DT)), "close": close, "open": norm_dt(pick(it, F_OPEN_DT)), "url": pick(it, F_URL),
            **base.get(id_, {}),
        })
    items = sorted(out.values(), key=lambda x: (x.get("close") or "9999", x["id"]))
    write_if_changed(DATA / "goods.json", dumps({"v": SCHEMA_VERSION, "updated_at": now.isoformat(timespec="minutes"), "items": items}))
    log(f"  진행중 물품 공고 {len(items)}건 → goods.json")


def step_thng_backfill(api, meta, tstore, now, checkpoint):
    """물품 과거 낙찰을 한 달씩 과거로 (기본 24개월). 진행: meta.thng_backfill {cursor, oldest, done}"""
    bf = meta.setdefault("thng_backfill", {})
    if bf.get("done"):
        return
    target = (now - dt.timedelta(days=round(30.44 * RECENT_FIRST_MONTHS))).strftime("%Y%m01")
    cursor = bf.get("cursor") or now.strftime("%Y%m%d")
    while True:
        cur = dt.datetime.strptime(cursor, "%Y%m%d").replace(tzinfo=KST)
        if cursor < target:
            bf["done"], bf["cursor"] = True, None
            log("[물품과거] 완료")
            return
        if api.remaining("scsbid") < THNG_RESERVE or api.remaining("bid") < THNG_BID_RESERVE or api.time_left() < 20:
            log(f"[물품과거] 오늘 몫 끝, 커서 {cursor}")
            return
        bgn = cur.replace(day=1, hour=0, minute=0)
        end = cur.replace(hour=23, minute=59)
        log(f"[물품과거] {bgn:%Y-%m-%d} ~ {end:%Y-%m-%d}")
        cnt = 0
        for it in api.fetch_range("thng_list", bgn, end):
            tstore.merge(it, None)
            cnt += 1
        # 기초금액은 공고 쪽이라 개찰보다 먼저 올라온다 — 과거로 가며 다음 달 처리 때 앞 달 개찰분도 채워진다
        for it in api.fetch_range("thng_notice", bgn, end):
            enrich_thng_notice(tstore, it)
        for it in api.fetch_range("thng_bsis", bgn, end):
            enrich_thng(tstore, it)
        cursor = bf["cursor"] = (bgn - dt.timedelta(days=1)).strftime("%Y%m%d")
        bf["oldest"] = bgn.strftime("%Y%m%d")
        log(f"  물품 낙찰 {cnt}건")
        checkpoint()


def step_region_fill(api, meta, store, now, checkpoint):
    """과거 낙찰 레코드에 참가가능지역(rgn)을 채운다. 참가수 예측에 쓰임(시·군 제한이면 참가가 적다).
    공고 게시일 기준 7일 단위로 최신 → 가장 오래된 낙찰 달 앞까지 한 번 훑는다 (한 달치는 호출이 많아 하루 한도에 끊겨도 이어지게).
    진행: meta.rgn_fill {cursor: YYYYMMDD(다음에 받을 구간의 끝 날), done}"""
    rf = meta.setdefault("rgn_fill", {})
    if rf.get("done"):
        return
    dates = [r["date"] for r in store.recs.values() if r.get("date")]
    if not dates:
        return
    d0 = dt.date.fromisoformat(sorted(dates)[len(dates) // 500])   # 드문 아주 옛 레코드는 무시
    oldest = (d0.replace(day=1) - dt.timedelta(days=1)).replace(day=1).strftime("%Y%m%d")   # 개찰 한 달 전 게시 공고까지
    cursor = rf.get("cursor") or now.strftime("%Y%m%d")
    if len(cursor) == 6:   # 예전 형식(YYYYMM)
        cursor = (dt.datetime.strptime(cursor + "01", "%Y%m%d") + dt.timedelta(days=32)).replace(day=1).strftime("%Y%m%d")
    while True:
        if cursor < oldest:
            rf["done"], rf["cursor"] = True, None
            log("[지역보강] 완료")
            return
        if api.remaining("bid") < REGION_RESERVE or api.time_left() < 20:
            log(f"[지역보강] 오늘 몫 끝, 커서 {cursor}")
            return
        end = min(dt.datetime.strptime(cursor, "%Y%m%d").replace(hour=23, minute=59, tzinfo=KST), now)
        bgn = (end - dt.timedelta(days=6)).replace(hour=0, minute=0)
        log(f"[지역보강] {bgn:%Y-%m-%d} ~ {end:%Y-%m-%d}")
        n = 0
        for it in api.fetch_range("notice_region", bgn, end):
            store.add_region(it)
            n += 1
        cursor = rf["cursor"] = (bgn - dt.timedelta(days=1)).strftime("%Y%m%d")
        log(f"  지역 {n}행")
        checkpoint()


def step_bsis_fill(api, meta, store, now, checkpoint):
    """과거 낙찰 중 기초금액 조회를 못 한 레코드(rng 없음 → A값을 모름)와 하한율 없는 레코드를 채운다.
    2026-09-27 확인: rng 없는 레코드는 A값을 몰라 1위 위치 계산이 공식 하한 미달 판정과 74%만 맞아 곡선·역검증에서 빠진다
    (2026-06 빈달 재수집분의 74% 등). 공고 게시·기초금액 등록일 7일 단위로 최신 → 가장 오래된 낙찰 달 앞까지 훑으며 빈 값만 채운다.
    진행: meta.bsis_fill {cursor: YYYYMMDD, filled} — 과거낙찰이 더 옛 달을 받으면 이어서 그 앞까지 간다."""
    bf = meta.setdefault("bsis_fill", {})
    dates = [r["date"] for r in store.recs.values() if r.get("date")]
    if not dates:
        return
    d0 = dt.date.fromisoformat(sorted(dates)[len(dates) // 500])
    oldest = (d0.replace(day=1) - dt.timedelta(days=62)).strftime("%Y%m%d")   # 개찰 두 달 전 게시 공고까지
    cursor = bf.get("cursor") or now.strftime("%Y%m%d")
    need = lambda r: not r.get("rng") or not r.get("floor")
    while cursor >= oldest:
        if api.remaining("bid") < REGION_RESERVE or api.time_left() < 20:
            log(f"[A값보강] 오늘 몫 끝, 커서 {cursor}")
            return
        end = min(dt.datetime.strptime(cursor, "%Y%m%d").replace(hour=23, minute=59, tzinfo=KST), now)
        bgn = (end - dt.timedelta(days=6)).replace(hour=0, minute=0)
        n = 0
        for op in ("notice_bsis", "notice_list"):
            for it in api.fetch_range(op, bgn, end):
                _, no, ord_ = notice_id(it)
                ids = [i for i in store.by_no.get(no, []) if need(store.recs[i])]
                if not ids:
                    continue
                if op == "notice_bsis":
                    v = clean({"base": to_int(pick(it, F_BASE)), "a": a_value(it), "net": net_cost(it), "rng": price_range(it)})
                    if not v.get("rng"):
                        continue
                else:
                    v = clean({"floor": to_rate(pick(it, F_FLOOR), 3)})
                for i in ids:
                    r = store.recs[i]
                    if r.get("ord") != ord_ and len(store.by_no.get(no, [])) > 1:
                        continue
                    before = dumps(r)
                    if op == "notice_bsis" and not r.get("rng"):   # 기초금액 조회를 못 했던 레코드 → 조회 결과로 (A값이 없으면 진짜 0)
                        r["rng"] = v["rng"]
                        for k in ("a", "net"):
                            if v.get(k):
                                r[k] = v[k]
                            else:
                                r.pop(k, None)
                        if not r.get("base") and v.get("base"):
                            r["base"] = v["base"]
                    elif op == "notice_list" and not r.get("floor") and v.get("floor"):
                        r["floor"] = v["floor"]
                    compute_sr(r)
                    if dumps(r) != before:
                        store.dirty = True
                        n += 1
        bf["filled"] = bf.get("filled", 0) + n
        cursor = bf["cursor"] = (bgn - dt.timedelta(days=1)).strftime("%Y%m%d")
        log(f"[A값보강] {bgn:%Y-%m-%d} ~ {end:%Y-%m-%d} 채움 {n}건")
        checkpoint()
    log(f"[A값보강] 끝까지 훑음 (누적 {bf.get('filled', 0)}건)")


def step_details(api, meta, store, ostore, regions, now, checkpoint, reserve=3, time_reserve=10):
    """reserve: 낙찰정보 서비스 호출을 이만큼 남기고 멈춘다 (뒤에 과거 수집이 쓸 몫)
    time_reserve: 남은 실행 시간(분)이 이만큼이면 멈춘다 — 운영계정(하루 10만)에선 한도보다 시간이 먼저 찬다"""
    target = meta.get("backfill", {}).get("target_start") or (now - dt.timedelta(days=365 * BACKFILL_YEARS)).strftime("%Y%m%d")
    tdate = f"{target[:4]}-{target[4:6]}-{target[6:8]}"
    dm = meta.setdefault("detail", {})
    dm["regions"] = regions
    queue = []
    pri = load_json(ROOT / "scripts" / "detail_priority.json", {})
    for sido in regions:
        ix = ostore.idx(sido)
        recs = [r for r in store.recs.values() if r.get("sido") == sido and (r.get("date") or "") >= tdate]
        if sido not in FULL_SIDOS:   # 관심 시·도 밖은 관심 면허 공고만 (전국 전부는 1년 수백 MB~1GB — 저장소 한도, 2026-09-26 검토)
            recs = [r for r in recs if set(pri.get("lic") or []) & set(r.get("lic") or [])]
        pending = [r for r in recs if r["id"] not in ix["done"] and ix["fail"].get(r["id"], 0) < DETAIL_MAX_TRIES]
        dm[sido] = {"total": len(recs), "done": sum(1 for r in recs if r["id"] in ix["done"]),
                    "failed": sum(1 for r in recs if ix["fail"].get(r["id"], 0) >= DETAIL_MAX_TRIES)}
        queue += [(sido, r) for r in pending]
    # 우선순위(scripts/detail_priority.json): 관심 시·도 → 관심 시·군 → 관심 면허 → 참가 적은 공고 순으로 먼저, 같은 등급은 최신부터
    p_sido, p_sgg, p_lic, p_cnt = set(pri.get("sido", [])), set(pri.get("sgg", [])), set(pri.get("lic", [])), pri.get("max_cnt") or 0

    def tier(r):   # 관심 시·도(강원) 전부 먼저 → 그다음 전국은 관심 면허·최신부터
        return ((r.get("sido") in p_sido) * 8 + (r.get("sgg") in p_sgg) * 4 + bool(p_lic & set(r.get("lic") or [])) * 2
                + bool(p_cnt and (r.get("cnt") or 10 ** 9) < p_cnt))

    queue.sort(key=lambda x: (tier(x[1]), x[1]["date"]), reverse=True)
    log(f"[상세] 대기 {len(queue)}건 (우선 {sum(1 for _, r in queue if tier(r) >= 4)}건)")
    done_now = 0
    for sido, rec in queue:
        if api.remaining("scsbid") < reserve or api.time_left() < time_reserve:
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


def step_thng_details(api, meta, tstore, tostore, now, checkpoint, minutes):
    """물품 개찰 순위 — 관심 시·도(detail_priority sido) 물품 낙찰 중 역검증에 쓸 수 있는 것(기초·하한율·예가범위·참가 2곳↑), 최근 24개월, 최신부터.
    공고 1건당 순위 조회 1~몇 회(참가 수에 따라 쪽 수). minutes 분까지만."""
    stop = time.time() + minutes * 60
    since = (now - dt.timedelta(days=round(30.44 * 24))).strftime("%Y-%m-%d")
    tm = meta.setdefault("thng_detail", {})
    queue = []
    if WATCH_BIZ and not meta.get("thng_mine_fix"):   # 전원 투찰률(x) 없이 받아 둔 우리 공고는 한 번 다시 받는다(2026-09-28)
        n_redo = 0
        for sido in sorted(FULL_SIDOS):
            ix = tostore.idx(sido)
            for year in sorted({v for v in ix["done"].values()}):
                y = tostore.year(sido, year)
                for bid_id, b in y["bids"].items():
                    if "x" not in b and any(y["corps"][r[1]][1] in WATCH_BIZ for r in b.get("r", [])):
                        ix["done"].pop(bid_id, None)
                        n_redo += 1
        meta["thng_mine_fix"] = True
        log(f"[물품상세] 우리 공고 {n_redo}건 전원 투찰률·복수예가로 다시 받음")
    for sido in sorted(FULL_SIDOS):
        ix = tostore.idx(sido)
        recs = [r for r in tstore.recs.values() if r.get("sido") == sido and (r.get("date") or "") >= since
                and r.get("base") and r.get("floor") and r.get("rng") and (r.get("cnt") or 0) >= 2]
        pending = [r for r in recs if r["id"] not in ix["done"] and ix["fail"].get(r["id"], 0) < DETAIL_MAX_TRIES]
        tm[sido] = {"total": len(recs), "done": sum(1 for r in recs if r["id"] in ix["done"]),
                    "failed": sum(1 for r in recs if ix["fail"].get(r["id"], 0) >= DETAIL_MAX_TRIES)}
        queue += [(sido, r) for r in pending]
    queue.sort(key=lambda x: x[1]["date"], reverse=True)
    log(f"[물품상세] 대기 {len(queue)}건")
    done_now = 0
    for sido, rec in queue:
        if time.time() > stop or api.remaining("scsbid") < 50 or api.time_left() < 10:
            break
        try:
            ranks = list(api.paged("opening_rank", {"bidNtceNo": rec["no"], "bidNtceOrd": rec["ord"]}))
        except ApiError as e:
            log(f"  ! {rec['id']} {e}")
            tostore.fail(sido, rec["id"])
            continue
        rb = [to_int(pick(i, F_RBID)) or 0 for i in ranks]
        if rb and max(rb) > 0:
            ranks = [i for i, n in zip(ranks, rb) if n == max(rb)]
        if not ranks:
            tostore.fail(sido, rec["id"])
            continue
        prices = []
        if any(re.sub(r"\D", "", str(pick(i, F_BIZ) or "")) in WATCH_BIZ for i in ranks):   # 우리 공고 = 복수예가까지(2026-09-28)
            try:
                prices = list(api.paged("thng_prepar", {"inqryDiv": "2", "bidNtceNo": rec["no"]}))
            except ApiError as e:
                log(f"  ! {rec['id']} 복수예가 {e}")
        tostore.add(sido, rec, ranks, prices)
        tm[sido]["done"] += 1
        done_now += 1
        if done_now % 200 == 0:
            checkpoint()
    log(f"  이번 실행 {done_now}건")


def step_thng_curve(api, meta, tstore, curve, now, checkpoint, minutes, tostore=None):
    """물품곡선(2026-09-28, 사용자 요청 '물품도 분석'): 전국 물품 낙찰 중 참가 50곳↑·기초·하한율·예가범위 있는 최근 THNG_CURVE_MONTHS(12)개월,
    최신부터 — 개찰 순위 전원 + 복수예가를 받아 추첨 평균 1순위 확률 곡선을 합계에만 더한다(원자료는 저장 안 함, scripts/thng_curve.py)."""
    stop = time.time() + minutes * 60
    months = int(os.environ.get("THNG_CURVE_MONTHS") or 12)
    since = (now - dt.timedelta(days=round(30.44 * months))).strftime("%Y-%m-%d")
    done, fail = curve.d["done"], curve.d["fail"]
    queue = [r for r in tstore.recs.values() if (r.get("date") or "") >= since and r.get("base") and r.get("floor") and r.get("rng")
             and (r.get("cnt") or 0) >= 50 and r["id"] not in done and fail.get(r["id"], 0) < DETAIL_MAX_TRIES]
    queue.sort(key=lambda r: r["date"], reverse=True)
    total = sum(1 for r in tstore.recs.values() if (r.get("date") or "") >= since and r.get("base") and r.get("floor") and r.get("rng") and (r.get("cnt") or 0) >= 50)
    meta["thng_curve"] = {"total": total, "done": sum(1 for r in tstore.recs.values() if r["id"] in done and (r.get("date") or "") >= since), "months": months}
    log(f"[물품곡선] 대기 {len(queue)}건 / 대상 {total}건")
    n_now = 0
    for rec in queue:
        if time.time() > stop or api.remaining("scsbid") < 50 or api.time_left() < 10:
            break
        try:
            ranks = list(api.paged("opening_rank", {"bidNtceNo": rec["no"], "bidNtceOrd": rec["ord"]}))
            prices = list(api.paged("thng_prepar", {"inqryDiv": "2", "bidNtceNo": rec["no"]}))
        except ApiError as e:
            log(f"  ! {rec['id']} {e}")
            fail[rec["id"]] = fail.get(rec["id"], 0) + 1
            continue
        rb = [to_int(pick(i, F_RBID)) or 0 for i in ranks]
        if rb and max(rb) > 0:
            ranks = [i for i, n in zip(ranks, rb) if n == max(rb)]
        amts, top = [], None
        for it in ranks:
            amt = to_int(pick(it, F_BID_AMT))
            if amt is None or re.sub(r"\D", "", str(pick(it, F_BIZ) or "")) in WATCH_BIZ:
                continue
            rank = to_int(pick(it, F_RANK)) or 0
            if rank == 1:
                top = amt
            if rank > 0 or "미달" in str(pick(it, F_NOTE) or ""):
                amts.append(amt)
        ps = sorted((to_int(pick(i, F_SNO)) or 0, to_int(pick(i, F_PRICE))) for i in prices if to_int(pick(i, F_PRICE)))
        base = next((to_int(pick(i, F_BASE)) for i in prices if pick(i, F_BASE)), None) or rec["base"]
        plan = next((to_int(pick(i, F_PLAN)) for i in prices if pick(i, F_PLAN)), None) or rec.get("plan")
        c = lottery_curve([p for _, p in ps], amts, base, rec["floor"])
        if c is None:
            fail[rec["id"]] = fail.get(rec["id"], 0) + 1
            continue
        n = rec.get("cnt") or len(ranks)
        rng = rec["rng"]
        key = f"{rec['date'][:7]}|{curve_bucket(n)}|{rng[0]:g},{rng[1]:g}|{'수의' if '수의' in (rec.get('cm') or '') else '경쟁'}"
        curve.add(key, n, c, real_window(plan, top, base, rec["floor"]))
        if any(re.sub(r"\D", "", str(pick(i, F_BIZ) or "")) in WATCH_BIZ for i in ranks):   # 우리가 넣은 공고는 개찰 상세(전원 투찰률·복수예가)도 저장 → my_bids·앱 개찰 결과
            if tostore is not None:
                tostore.add(rec.get("sido") or "기타", rec, ranks, prices)
        done[rec["id"]] = 1
        fail.pop(rec["id"], None)
        n_now += 1
        if n_now % 200 == 0:
            checkpoint()
    meta["thng_curve"]["done"] += n_now
    log(f"  이번 실행 {n_now}건")


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
    tstore = ThngStore()
    ostore = OpeningStore()
    tostore = ThngOpeningStore()
    tcurve = ThngCurve(DATA / "thng_curve.json")
    oldstore = OldStore()
    oostore = OldOpeningStore()
    errors = []

    def save_all(final=False):
        cache.save()
        corp_info().save()
        ostore.save()
        tostore.save()
        tcurve.save()
        files_old, counts_old = oldstore.save()
        oostore.save()
        files_oo, _ = oostore.files()
        files_s, counts_s = store.save()
        files_t, counts_t = tstore.save()
        files_o, counts_o = ostore.files()
        files_to, counts_to = tostore.files()
        bids = cache.bids(now)
        changed = write_if_changed(DATA / "bids.json", dumps({"items": bids, "v": SCHEMA_VERSION}))
        changed = write_lic_map(cache) or changed
        old_files = meta.get("files", {})
        meta["files"] = {"scsbid": files_s, "opening": files_o, "thng": files_t, "opening_thng": files_to, "scsbid_old": files_old, "opening_old": files_oo}
        meta["counts"] = {"bids": len(bids), "scsbid": counts_s, "opening": counts_o,
                          "scsbid_total": sum(counts_s.values()), "thng": counts_t, "thng_total": sum(counts_t.values()), "opening_thng": counts_to}
        meta["v"] = SCHEMA_VERSION
        if changed or store.dirty or tstore.dirty or ostore.dirty or old_files != meta["files"]:
            meta["updated_at"] = now_kst().isoformat(timespec="seconds")
        meta["last_run"] = {"at": now_kst().isoformat(timespec="seconds"),
                            "minutes": round((time.time() - started) / 60, 1),
                            "calls": dict(api.calls), "errors": errors[-20:]}
        write_if_changed(DATA / "meta.json", json.dumps(meta, ensure_ascii=False, indent=1, sort_keys=True))
        store.dirty = tstore.dirty = False
        if final:
            log(f"저장 완료: 공고 {len(bids)}건, 낙찰 {sum(counts_s.values())}건, 상세 {sum(counts_o.values())}건")

    # 순서: 공고 → 최근 낙찰 → 개찰 상세(최신부터, 과거 수집 몫은 남김) → 과거 24개월 → 남은 한도로 상세 → 나머지 과거
    # 상세는 낙찰정보 서비스만 쓰고 과거 수집은 주로 입찰공고 서비스를 써서, 같이 돌려도 서로 크게 방해하지 않는다
    horizon = now - dt.timedelta(days=round(30.44 * RECENT_FIRST_MONTHS))
    steps = [
        ("공고", lambda: step_notices(api, meta, cache, now)),
        ("최근낙찰", lambda: step_recent_scsbid(api, meta, store, cache, now)),
        ("공고문", lambda: step_doc_flags(api, cache, now, float(os.environ.get("DOC_MINUTES") or 8))),   # 사실조사(사전단속) 표시 — 공고문 첨부에서 (2026-09-27)
        ("상세", lambda: step_details(api, meta, store, ostore, regions, now, save_all, reserve=DETAIL_RESERVE, time_reserve=MAX_MINUTES * 0.55)),   # 첫 상세는 실행 시간의 45%까지만
        ("물품최근", lambda: step_thng_recent(api, meta, tstore, now, cache)),   # 앱 '물품' 공고(goods.json) — 가볍고 매일 필요해서 앞에
        ("빈달", lambda: step_refill(api, meta, store, now, save_all)),
        ("물품과거", lambda: step_thng_backfill(api, meta, tstore, now, save_all)),
        ("물품상세", lambda: step_thng_details(api, meta, tstore, tostore, now, save_all, float(os.environ.get("THNG_DETAIL_MINUTES") or 40))),   # 물품 개찰 순위(관심 시·도) — 물품 역검증용 (2026-09-27)
        ("물품곡선", lambda: step_thng_curve(api, meta, tstore, tcurve, now, save_all, float(os.environ.get("THNG_CURVE_MINUTES") or 50), tostore)),   # 전국 물품 참가 50곳↑ 추첨 평균 곡선 (2026-09-28)
        ("지역보강", lambda: step_region_fill(api, meta, store, now, save_all)),
        ("A값보강", lambda: step_bsis_fill(api, meta, store, now, save_all)),   # 2026-09-27: 기초금액 조회가 빠진 과거 낙찰(A값 모름)을 다시 채워 곡선·역검증 표본을 늘림
        ("과거낙찰", lambda: step_backfill(api, meta, store, now, save_all, horizon)),
        ("상세", lambda: step_details(api, meta, store, ostore, regions, now, save_all)),
        ("과거낙찰", lambda: step_backfill(api, meta, store, now, save_all)),
        ("옛낙찰", lambda: step_old_backfill(api, meta, oldstore, now, save_all)),   # 3년보다 옛 낙찰(강원만, 업체 이력용) — 과거낙찰이 끝난 뒤 (2026-09-28)
        ("옛상세", lambda: step_old_details(api, meta, oldstore, oostore, now, save_all, float(os.environ.get("OLD_DETAIL_MINUTES") or 40))),   # 옛 강원 공사 개찰 상세(압축) — 업체 보기용 (2026-09-28)
        ("업체정보", lambda: step_corp_info(api, meta, now, save_all)),   # 대표자·주소 채우기는 남는 한도로 (2026-09-27: 앞에 두었더니 물품·과거 수집 몫을 다 씀)
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
            errors.append(hide_key(f"{name}: 한도 소진 ({e})"))
        except FatalApiError as e:
            log(f"[{name}] 치명적 오류: {e}")
            errors.append(hide_key(f"{name}: {e}"))
            fatal = e
            break
        except Exception as e:  # 한 단계가 죽어도 나머지는 진행
            log(traceback.format_exc())
            errors.append(hide_key(f"{name}: {type(e).__name__}: {e}"))
    save_all(final=True)
    if fatal:
        sys.exit(2)


if __name__ == "__main__":
    main()

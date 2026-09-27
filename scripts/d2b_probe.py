"""국방전자조달(D2B) 오퍼레이션 탐색 — 이름·조회 조건·필드를 모를 때 수집 서버(Actions)에서 한 번 돌려 data/d2b/_probe.json 에 남긴다.

클라우드 작업 세션은 d2b.go.kr·data.go.kr 에 접속할 수 없어(허용 목록 밖) 스펙을 직접 못 본다 → 여기서 후보를 불러 보고
결과(resultCode·totalCount·첫 항목 필드)를 저장, 다음 세션이 그걸 보고 collect_d2b.py 를 고친다.
이미 _probe.json 이 있으면 아무것도 안 함(PROBE_FORCE=1 이면 다시).
"""
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from collect_d2b import BASES, OUT, KST, dumps  # noqa: E402

KEY = (os.environ.get("DATA_GO_KR_KEY") or "").strip()
PATH = OUT / "_probe.json"
calls = 0


def get(base, svc, op, **p):
    global calls
    q = {k: v for k, v in p.items() if v not in (None, "")}
    if base == 1:
        q["serviceKey"] = KEY
    url = BASES[base] + svc + "/" + op + "?" + urllib.parse.urlencode(q)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (bid-predictor collector)"})
        with urllib.request.urlopen(req, timeout=60) as r:
            t = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:   # 게이트웨이는 오류 원문(오퍼레이션 없음·권한 없음 등)을 본문에 줌
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return {"err": f"HTTP {e.code}", "raw": re.sub(r"\s+", " ", body[:300])}
    except Exception as e:  # noqa: BLE001
        return {"err": str(e)[:200]}
    calls += 1
    code = re.search(r"<resultCode>([^<]*)", t)
    msg = re.search(r"<resultMsg>([^<]*)", t)
    items = [dict(re.findall(r"<(\w+)>([^<]*)</\1>", it)) for it in re.findall(r"<item>(.*?)</item>", t, re.S)]
    tc = re.search(r"<totalCount>(\d+)", t)
    out = {"code": code.group(1) if code else None, "msg": msg.group(1) if msg else None,
           "total": int(tc.group(1)) if tc else None, "n": len(items)}
    if items:
        out["item"] = items[0]
    if not code:
        out["raw"] = re.sub(r"\s+", " ", t[:400])
    return out, items


def try_op(svc, op, variants, base_order=(0, 1)):
    """조건 후보를 차례로 — 결과가 있는 첫 조건에서 멈춤. 기록: [{base, params, …}]"""
    log, first = [], []
    for base in base_order:
        if base == 1 and not KEY:
            continue
        for p in variants:
            r = get(base, svc, op, **p)
            if isinstance(r, dict):   # 접속 실패
                log.append({"base": base, "params": p, **r})
                break                 # 이 주소는 안 됨 → 다음 주소
            info, items = r
            log.append({"base": base, "params": p, **info})
            if items:
                return log, items
            first = first or items
        if log and log[-1].get("code") is not None:
            break                     # 주소는 응답함(조건만 틀림) → 다른 주소는 안 봐도 됨
    return log, first


def main():
    if PATH.exists() and os.environ.get("PROBE_FORCE") != "1":
        print("탐색 결과 이미 있음:", PATH)
        return
    now = dt.datetime.now(KST)
    d = lambda days: (now + dt.timedelta(days=days)).strftime("%Y%m%d")  # noqa: E731
    past, today, ahead = d(-30), d(0), d(60)
    res = {"at": now.isoformat(timespec="seconds")}

    # ① 공개수의 결과: 목록 1건 → 참가업체·복수예가를 조건 여러 가지로 (지금 참가업체가 비어 3,453건이 버려짐)
    for kind, pre in (("N", "getDmstcOthbcVltrnNtatResult"), ("NF", "getFcltyOthbcVltrnNtatResult")):
        log, items = try_op("BidResultInfoService", pre + "List", [dict(numOfRows=3, pageNo=1, ntatComptDateBegin=past, ntatComptDateEnd=today)])
        res[kind + "_list"] = log
        it = next((x for x in items if "1순위" in json.dumps(x, ensure_ascii=False) or (x.get("ntatResult") or "").strip()), items[0] if items else None)
        if not it:
            continue
        base = log[-1]["base"]
        keys = ["demandYear", "orntCode", "dcsNo", "iemNo", "cntrwkNo", "pblancNo", "pblancOdr"]
        k = {x: it.get(x) for x in keys if it.get(x)}
        dates = {x: it.get(x) for x in it if re.search(r"(Date|Dt)$", x) and it.get(x)}
        res[kind + "_dates"] = dates
        day = next((re.sub(r"\D", "", v)[:8] for v in dates.values() if len(re.sub(r"\D", "", v)) >= 8), None)
        variants = [dict(numOfRows=5, **k), dict(numOfRows=5, ntatPlanDate=day, **k), dict(numOfRows=5, opengDate=day, **k),
                    dict(numOfRows=5, ntatComptDate=day, **k),
                    dict(numOfRows=5, ntatPlanDate=day, **{x: v for x, v in k.items() if x not in ("pblancNo", "pblancOdr")}),
                    dict(numOfRows=5, opengDate=day, **{x: v for x, v in k.items() if x not in ("pblancNo", "pblancOdr")})]
        for part in ("Detail", "MnufList", "BsicList"):
            res[f"{kind}_{part}"] = try_op("BidResultInfoService", pre + part, variants, (base,))[0]

    # ② 공개수의 진행 공고 (우리 공고 탭 후보) — 이름·날짜 조건 후보
    for op in ("getDmstcOthbcVltrnNtatPblancList", "getFcltyOthbcVltrnNtatPblancList"):
        res[op] = try_op("BidPblancInfoService", op, [
            dict(numOfRows=3, pageNo=1, ntatPlanDateBegin=today, ntatPlanDateEnd=ahead),
            dict(numOfRows=3, pageNo=1, opengDateBegin=today, opengDateEnd=ahead),
            dict(numOfRows=3, pageNo=1, pblancDateBegin=past, pblancDateEnd=today),
            dict(numOfRows=3, pageNo=1)])[0]

    res["calls"] = calls
    PATH.write_text(dumps(res), encoding="utf-8")
    print("탐색 끝:", calls, "회 →", PATH)


if __name__ == "__main__":
    main()

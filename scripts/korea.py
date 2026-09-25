"""지역(시도·시군구)과 면허(업종) 이름 정규화.

collect.py 에서만 사용한다. 앱(app.js)은 여기서 정규화된 값만 받는다.
"""
import re

# ---------------------------------------------------------------- 시·도
SIDO_FULL = {
    "서울": ["서울특별시"],
    "부산": ["부산광역시"],
    "대구": ["대구광역시"],
    "인천": ["인천광역시"],
    "광주": ["광주광역시"],
    "대전": ["대전광역시"],
    "울산": ["울산광역시"],
    "세종": ["세종특별자치시"],
    "경기": ["경기도"],
    "강원": ["강원특별자치도", "강원도"],
    "충북": ["충청북도"],
    "충남": ["충청남도"],
    "전북": ["전북특별자치도", "전라북도"],
    "전남": ["전라남도"],
    "경북": ["경상북도"],
    "경남": ["경상남도"],
    "제주": ["제주특별자치도", "제주도"],
}
SIDO_ORDER = list(SIDO_FULL.keys())

_SGG_RAW = {
    "서울": "종로구 중구 용산구 성동구 광진구 동대문구 중랑구 성북구 강북구 도봉구 노원구 은평구 서대문구 마포구 양천구 강서구 구로구 금천구 영등포구 동작구 관악구 서초구 강남구 송파구 강동구",
    "부산": "중구 서구 동구 영도구 부산진구 동래구 남구 북구 해운대구 사하구 금정구 강서구 연제구 수영구 사상구 기장군",
    "대구": "중구 동구 서구 남구 북구 수성구 달서구 달성군 군위군",
    "인천": "중구 동구 미추홀구 연수구 남동구 부평구 계양구 서구 강화군 옹진군",
    "광주": "동구 서구 남구 북구 광산구",
    "대전": "동구 중구 서구 유성구 대덕구",
    "울산": "중구 남구 동구 북구 울주군",
    "세종": "세종시",
    "경기": "수원시 성남시 의정부시 안양시 부천시 광명시 평택시 동두천시 안산시 고양시 과천시 구리시 남양주시 오산시 시흥시 군포시 의왕시 하남시 용인시 파주시 이천시 안성시 김포시 화성시 광주시 양주시 포천시 여주시 연천군 가평군 양평군",
    "강원": "춘천시 원주시 강릉시 동해시 태백시 속초시 삼척시 홍천군 횡성군 영월군 평창군 정선군 철원군 화천군 양구군 인제군 고성군 양양군",
    "충북": "청주시 충주시 제천시 보은군 옥천군 영동군 증평군 진천군 괴산군 음성군 단양군",
    "충남": "천안시 공주시 보령시 아산시 서산시 논산시 계룡시 당진시 금산군 부여군 서천군 청양군 홍성군 예산군 태안군",
    "전북": "전주시 군산시 익산시 정읍시 남원시 김제시 완주군 진안군 무주군 장수군 임실군 순창군 고창군 부안군",
    "전남": "목포시 여수시 순천시 나주시 광양시 담양군 곡성군 구례군 고흥군 보성군 화순군 장흥군 강진군 해남군 영암군 무안군 함평군 영광군 장성군 완도군 진도군 신안군",
    "경북": "포항시 경주시 김천시 안동시 구미시 영주시 영천시 상주시 문경시 경산시 의성군 청송군 영양군 영덕군 청도군 고령군 성주군 칠곡군 예천군 봉화군 울진군 울릉군",
    "경남": "창원시 진주시 통영시 사천시 김해시 밀양시 거제시 양산시 의령군 함안군 창녕군 고성군 남해군 하동군 산청군 함양군 거창군 합천군",
    "제주": "제주시 서귀포시",
}
SGG = {k: v.split() for k, v in _SGG_RAW.items()}

# 시군구 이름 → 시도 (전국에서 유일한 이름만)
_sgg_count = {}
for _sd, _names in SGG.items():
    for _n in _names:
        _sgg_count.setdefault(_n, []).append(_sd)
SGG_UNIQUE = {n: sds[0] for n, sds in _sgg_count.items() if len(sds) == 1}

# "춘천교육지원청"처럼 '시/군' 없이 쓰인 줄임말 (전국 유일 + 시도 약칭과 겹치지 않는 것만)
_stem_count = {}
for _sd, _names in SGG.items():
    for _n in _names:
        if _n[-1] in "시군" and len(_n) >= 3:
            _stem_count.setdefault(_n[:-1], set()).add((_sd, _n))
STEM_UNIQUE = {
    s: next(iter(v)) for s, v in _stem_count.items()
    if len(v) == 1 and s not in SIDO_FULL
}
_STEM_RE = re.compile(
    r"(?<![가-힣])(" + "|".join(sorted(map(re.escape, STEM_UNIQUE), key=len, reverse=True)) + ")"
    r"(?=교육지원청|교육청|시청|군청|지사|지역|지청|소방서|경찰서|우체국|세무서|지원|사무소|시|군)"
)
_SHORT_RE = re.compile(
    r"(?:^|[\s(\[·,])(" + "|".join(SIDO_FULL) + r")(?!장|광역|특별)"
)


def parse_region(*texts):
    """여러 텍스트(공사현장, 참가지역, 기관명…)를 우선순위대로 보고 (시도, 시군구)를 돌려준다."""
    sido = sgg = None
    for text in texts:
        if not text:
            continue
        t = str(text)
        s, g = _parse_one(t)
        if s and not sido:
            sido = s
        if sido and s == sido and g and not sgg:
            sgg = g
        if sido and sgg:
            break
    if sido and not sgg:
        for text in texts:
            if text:
                g = _find_sgg(str(text), sido)
                if g:
                    sgg = g
                    break
    return sido, sgg


def _parse_one(t):
    # 1) 시도 정식 명칭
    best = None
    for sd, fulls in SIDO_FULL.items():
        for f in fulls:
            i = t.find(f)
            if i >= 0 and (best is None or i < best[0]):
                best = (i, sd)
    if best:
        sd = best[1]
        return sd, _find_sgg(t, sd)
    # 2) 전국 유일 시군구 이름
    for name, sd in SGG_UNIQUE.items():
        if len(name) >= 3 and name in t:
            return sd, name
    # 3) 시도 약칭 (서울, 강원 …)
    m = _SHORT_RE.search(t)
    if m:
        sd = m.group(1)
        return sd, _find_sgg(t, sd)
    # 4) 줄임말 (춘천교육지원청 → 강원 춘천시)
    m = _STEM_RE.search(t)
    if m:
        sd, name = STEM_UNIQUE[m.group(1)]
        return sd, name
    return None, None


def _find_sgg(t, sido):
    for name in sorted(SGG.get(sido, []), key=len, reverse=True):
        if name in t:
            return name
    for name in SGG.get(sido, []):
        if name[-1] in "시군" and len(name) >= 3 and name[:-1] in t and name[:-1] not in SIDO_FULL:
            return name
    return None


# ---------------------------------------------------------------- 면허 (2022 대업종화 기준 23개)
LICENSES = [
    "토목", "건축", "토목건축", "산업환경설비", "조경",
    "지반조성·포장", "실내건축", "금속창호·지붕건축물조성", "도장·습식·방수·석공",
    "조경식재·시설물", "철근·콘크리트", "구조물해체·비계", "상하수도설비",
    "보링·그라우팅", "철도·궤도", "철강구조물", "수중·준설", "승강기·삭도",
    "전기", "정보통신", "소방시설", "기계설비", "가스시설시공",
]

# 별칭표: 옛 명칭·표기 변형 → 정식 면허. 비교 전 공백·가운뎃점 등은 모두 제거한다.
LICENSE_ALIASES = {
    "토목": ["토목공사업", "토목공사"],
    "건축": ["건축공사업", "건축공사"],
    "토목건축": ["토목건축공사업", "토목건축공사"],
    "산업환경설비": ["산업환경설비공사업", "산업환경설비"],
    "조경": ["조경공사업", "조경공사"],
    "지반조성·포장": ["지반조성포장공사업", "지반조성포장", "토공사업", "토공사", "포장공사업", "포장공사"],
    "실내건축": ["실내건축공사업", "실내건축공사", "실내건축"],
    "금속창호·지붕건축물조성": [
        "금속창호지붕건축물조성공사업", "금속창호지붕건축물조성", "금속구조물창호온실공사업",
        "금속구조물창호온실", "금속구조물창호", "지붕판금건축물조립공사업", "지붕판금건축물조립",
    ],
    "도장·습식·방수·석공": [
        "도장습식방수석공사업", "도장습식방수석공", "도장공사업", "도장공사", "습식방수공사업",
        "습식방수", "석공사업", "석공사", "미장방수조적공사업", "미장방수조적",
    ],
    "조경식재·시설물": [
        "조경식재시설물공사업", "조경식재시설물", "조경식재공사업", "조경식재",
        "조경시설물설치공사업", "조경시설물설치", "조경시설물",
    ],
    "철근·콘크리트": ["철근콘크리트공사업", "철근콘크리트"],
    "구조물해체·비계": ["구조물해체비계공사업", "구조물해체비계", "비계구조물해체공사업", "비계구조물해체"],
    "상하수도설비": ["상하수도설비공사업", "상하수도설비", "상하수도"],
    "보링·그라우팅": ["보링그라우팅파일공사업", "보링그라우팅공사업", "보링그라우팅"],
    "철도·궤도": ["철도궤도공사업", "철도궤도", "궤도공사업", "궤도공사"],
    "철강구조물": ["철강구조물공사업", "철강구조물", "강구조물공사업", "강구조물", "철강재설치공사업", "철강재설치"],
    "수중·준설": ["수중준설공사업", "수중준설", "수중공사업", "준설공사업"],
    "승강기·삭도": ["승강기삭도공사업", "승강기삭도", "승강기설치공사업", "승강기설치", "삭도설치공사업", "삭도설치"],
    "전기": ["전기공사업", "전기공사"],
    "정보통신": ["정보통신공사업", "정보통신공사", "정보통신"],
    "소방시설": ["전문소방시설공사업", "일반소방시설공사업", "소방시설공사업", "소방시설공사", "소방시설"],
    "기계설비": ["기계가스설비공사업", "기계가스설비", "기계설비공사업", "기계설비"],
    "가스시설시공": ["가스시설시공업", "가스시설시공", "난방시공업"],
}
_SQUASH_RE = re.compile(r"[\s·ㆍ・.,\-()\[\]{}\d/_:;'\"‧]+")


def _squash(s):
    return _SQUASH_RE.sub("", s)


_ALIAS_LIST = sorted(
    ((_squash(a), lic) for lic, al in LICENSE_ALIASES.items() for a in al),
    key=lambda x: len(x[0]), reverse=True,
)
_BARE = {_squash(l): l for l in LICENSES}


def normalize_licenses(*texts):
    """면허 제한 문자열들 → 정식 면허 이름 목록(중복 제거, 23개 순서)."""
    found = set()
    for text in texts:
        if not text:
            continue
        if isinstance(text, (list, tuple)):
            for t in text:
                found.update(normalize_licenses(t))
            continue
        for token in re.split(r"[,|;\n/]|또는|및|\bor\b", str(text)):
            sq = _squash(token)
            if not sq:
                continue
            hit = False
            for alias, lic in _ALIAS_LIST:
                if alias in sq:
                    found.add(lic)
                    sq = sq.replace(alias, "|")
                    hit = True
            if not hit:
                bare = re.sub(r"(공사업|공사|업)$", "", sq)
                if bare in _BARE:
                    found.add(_BARE[bare])
    return [l for l in LICENSES if l in found]

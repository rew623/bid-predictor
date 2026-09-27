"""공고문 첨부파일(hwp·hwpx·pdf)에서 글자를 뽑아 특정 문구가 있는지 본다 — 조달청 API 에 없는 정보용.

지금 쓰는 곳: 사실조사(사전단속) 공사 표시(collect.py step_doc_flags → bids.json sd=1, 앱 카드 태그).
더비스가 '사실조사(사전단속)가 실시되는 공사입니다'로 보여 주는 것과 같은 정보 — 공고문 첫머리 안내 상자에 있다(2026-09-27 한국철도공사 공고 확인).
"""
import io
import re
import struct
import zipfile
import zlib

FLAGS = {"sd": re.compile(r"사전\s*단속|상시\s*단속")}   # 사실조사(사전단속) — 국토부 '부적격 건설사업자 상시 단속 가이드라인'


def hwp_text(data):
    import olefile
    ole = olefile.OleFileIO(io.BytesIO(data))
    hdr = ole.openstream("FileHeader").read()
    compressed = bool(struct.unpack("<I", hdr[36:40])[0] & 1)
    out = []
    if ole.exists("PrvText"):   # 미리보기 글(앞부분) — 안내 상자는 대개 여기서 잡힌다
        out.append(ole.openstream("PrvText").read().decode("utf-16-le", "ignore"))
    secs = sorted((e for e in ole.listdir() if len(e) == 2 and e[0] == "BodyText"), key=lambda e: int(re.sub(r"\D", "", e[1]) or 0))
    for e in secs:
        raw = ole.openstream(e).read()
        if compressed:
            try:
                raw = zlib.decompress(raw, -15)
            except zlib.error:
                continue
        i = 0
        while i + 4 <= len(raw):
            h = struct.unpack("<I", raw[i:i + 4])[0]
            tag, size = h & 0x3FF, (h >> 20) & 0xFFF
            i += 4
            if size == 0xFFF:
                size = struct.unpack("<I", raw[i:i + 4])[0]
                i += 4
            if tag == 67:   # HWPTAG_PARA_TEXT
                out.append(raw[i:i + size].decode("utf-16-le", "ignore"))
            i += size
    return "\n".join(out)


def hwpx_text(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    return "\n".join(re.sub(r"<[^>]+>", "", z.read(n).decode("utf-8", "ignore")) for n in z.namelist() if n.startswith("Contents/section"))


def pdf_text(data):
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in r.pages[:6])   # 안내는 앞쪽에 있다


def text_of(data, name=""):
    n = (name or "").lower()
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return hwp_text(data)
    if data[:2] == b"PK":
        return hwpx_text(data)
    if data[:4] == b"%PDF" or n.endswith(".pdf"):
        return pdf_text(data)
    return ""


def flags_of(text):
    t = re.sub(r"[\x00-\x1f]", "", text)
    return {k: 1 for k, rx in FLAGS.items() if rx.search(t)}

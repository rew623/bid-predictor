#!/usr/bin/env python3
"""안정판 만들기: 지정한 커밋(버전)의 앱 파일을 stable/ 에 복사하고, 최신 data/ 를 읽도록 경로만 고친다.
새 버전에 문제가 생기면 설정의 '안정판으로 열기'(stable/)로 예전 화면을 그대로 쓸 수 있다.

사용: python scripts/make_stable.py <커밋 또는 태그> [<버전 이름>]
예:   python scripts/make_stable.py 7314795 1.7.5
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "stable"


def show(ref, path):
    return subprocess.run(["git", "show", f"{ref}:{path}"], cwd=ROOT, capture_output=True, check=True).stdout.decode("utf-8")


def main():
    ref = sys.argv[1]
    ver = sys.argv[2] if len(sys.argv) > 2 else (re.search(r"VERSION = '([^']+)'", show(ref, "sw.js")) or [None, ref])[1]
    OUT.mkdir(exist_ok=True)

    app = show(ref, "app.js")
    for a, b in [("url(path, ver=true){ return 'data/' +", "url(path, ver=true){ return (window.DATA_BASE || 'data/') +"),
                 ("if(!('serviceWorker' in navigator)) return;", "if(!('serviceWorker' in navigator) || window.STABLE) return;   // 안정판은 서비스워커를 따로 쓰지 않음")]:
        assert app.count(a) == 1, f"{ref} app.js 에 '{a[:30]}' 가 없음"
        app = app.replace(a, b)

    html = show(ref, "index.html")
    html = (html.replace('href="manifest.json"', 'href="../manifest.json"')
                .replace('href="icons/', 'href="../icons/').replace('src="icons/', 'src="../icons/'))
    banner = (f'<style>body{{padding-top:38px}}</style><div style="position:fixed;top:0;left:0;right:0;z-index:1000;background:#FEF0C7;color:#B54708;padding:8px 14px;font-size:13.5px;text-align:center">'
              f'🛟 안정판 {ver} 사용 중 — 새 버전에 문제가 있을 때 쓰는 예전 화면입니다. '
              f'<a href="../" style="color:#1D56C9;font-weight:600">최신판으로 돌아가기</a></div>')
    html = re.sub(r"(<body[^>]*>)", r"\1\n" + banner.replace("\\", "\\\\"), html, count=1)
    html = html.replace('<script src="app.js"></script>',
                        f'<script>window.DATA_BASE = "../data/"; window.STABLE = "{ver}";</script>\n<script src="app.js"></script>')

    (OUT / "app.js").write_text(app, encoding="utf-8", newline="")
    (OUT / "index.html").write_text(html, encoding="utf-8", newline="")
    (OUT / "style.css").write_text(show(ref, "style.css"), encoding="utf-8", newline="")
    (OUT / "VERSION").write_text(f"{ver}\n{ref}\n", encoding="utf-8")
    print(f"stable/ ← {ref} (버전 {ver})")


if __name__ == "__main__":
    main()

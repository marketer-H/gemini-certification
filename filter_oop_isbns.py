#!/usr/bin/env python3
"""
절판 ISBN을 isbns.txt에서 제거
알라딘에서 절판 여부 확인
"""

import re
import requests
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = Path(__file__).parent
ISBNS_FILE = BASE_DIR / "isbns.txt"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HTML_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Accept-Encoding": "identity",
}

print_lock = threading.Lock()


def is_oop(isbn: str) -> tuple:
    """알라딘에서 판매 여부 확인. (isbn, 판매불가여부, 제목) 반환"""
    url = f"https://www.aladin.co.kr/shop/wproduct.aspx?ISBN={isbn}"
    try:
        r = requests.get(url, headers=HTML_HEADERS, timeout=15)
        if r.status_code != 200:
            return isbn, False, ""
        text = r.text
        t = re.search(r'<meta property="og:title" content="([^"]+)"', text)
        title = t.group(1).split(" | ")[0].strip() if t else isbn

        # 구매 버튼이 있으면 판매중
        on_sale = bool(re.search(r'장바구니 담기|바로구매|btn_buy', text))
        return isbn, not on_sale, title
    except Exception:
        return isbn, False, ""


def main():
    isbns = [l.strip() for l in ISBNS_FILE.read_text().splitlines() if l.strip()]
    print(f"총 {len(isbns)}개 ISBN 절판 여부 확인 중...\n")

    results = {}
    done = [0]

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(is_oop, isbn): isbn for isbn in isbns}
        for f in as_completed(futures):
            isbn, oop, title = f.result()
            results[isbn] = (oop, title)
            done[0] += 1
            status = "판매불가" if oop else "판매중"
            with print_lock:
                print(f"[{done[0]}/{len(isbns)}] {isbn}  {status}  {title}")

    remove = [(isbn, results[isbn][1]) for isbn in isbns if results[isbn][0]]
    keep   = [isbn for isbn in isbns if not results[isbn][0]]

    print(f"\n{'='*50}")
    print(f"판매불가 도서 ({len(remove)}건):")
    for isbn, title in remove:
        print(f"  {isbn}  {title}")
    print(f"\n유지: {len(keep)}건 / 삭제: {len(remove)}건")
    print(f"{'='*50}")

    if not remove:
        print("판매불가 도서 없음.")
        return

    confirm = input("\nisbns.txt에서 삭제할까요? (y/n): ").strip().lower()
    if confirm == "y":
        ISBNS_FILE.write_text("\n".join(keep) + "\n")
        print(f"완료. {len(remove)}개 삭제, {len(keep)}개 유지.")
    else:
        print("취소.")


if __name__ == "__main__":
    main()

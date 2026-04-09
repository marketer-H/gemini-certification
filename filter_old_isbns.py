#!/usr/bin/env python3
"""
출간연도 2022년 이하 ISBN을 isbns.txt에서 제거
알라딘에서 출간연도 조회
"""

import re
import time
import requests
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

CUT_YEAR = 2022  # 이 연도 이하 삭제


def get_pub_year(isbn: str) -> tuple:
    """알라딘에서 출간연도 조회. (isbn, year or None) 반환"""
    url = f"https://www.aladin.co.kr/shop/wproduct.aspx?ISBN={isbn}"
    try:
        r = requests.get(url, headers=HTML_HEADERS, timeout=15)
        if r.status_code != 200:
            return isbn, None
        # JSON-LD pubDate
        m = re.search(r'"pubDate"\s*:\s*"(\d{4})', r.text)
        if m:
            return isbn, int(m.group(1))
        # "2023년 1월" 형태
        m = re.search(r'(\d{4})년\s*\d+월', r.text)
        if m:
            return isbn, int(m.group(1))
        return isbn, None
    except Exception:
        return isbn, None


def main():
    isbns = [line.strip() for line in ISBNS_FILE.read_text().splitlines() if line.strip()]
    print(f"총 {len(isbns)}개 ISBN 출간연도 조회 중...\n")

    results = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(get_pub_year, isbn): isbn for isbn in isbns}
        done = 0
        for f in as_completed(futures):
            isbn, year = f.result()
            results[isbn] = year
            done += 1
            status = str(year) if year else "연도 미확인"
            print(f"[{done}/{len(isbns)}] {isbn}  {status}")

    keep = []
    remove = []
    unknown = []

    for isbn in isbns:
        year = results.get(isbn)
        if year is None:
            unknown.append(isbn)
            keep.append(isbn)  # 연도 미확인은 일단 유지
        elif year <= CUT_YEAR:
            remove.append((isbn, year))
        else:
            keep.append(isbn)

    print(f"\n{'='*50}")
    print(f"삭제 대상 ({len(remove)}건, {CUT_YEAR}년 이하):")
    for isbn, year in sorted(remove, key=lambda x: x[1]):
        print(f"  {isbn}  ({year}년)")

    if unknown:
        print(f"\n연도 미확인 (유지, {len(unknown)}건):")
        for isbn in unknown:
            print(f"  {isbn}")

    print(f"\n유지: {len(keep)}건 / 삭제: {len(remove)}건")
    print(f"{'='*50}")

    confirm = input("\nisbns.txt에서 삭제할까요? (y/n): ").strip().lower()
    if confirm == "y":
        ISBNS_FILE.write_text("\n".join(keep) + "\n")
        print(f"완료. {len(remove)}개 삭제, {len(keep)}개 유지.")
    else:
        print("취소.")


if __name__ == "__main__":
    main()

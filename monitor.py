#!/usr/bin/env python3
"""
도서 평점 모니터: 교보문고 / 예스24 / 알라딘
평점이 threshold 아래로 내려가면 알림 발송
"""

import json
import re
import sys
import time
import smtplib
import warnings
from pathlib import Path
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

warnings.filterwarnings("ignore")

# ─── 파일 경로 ────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
ISBNS_FILE = BASE_DIR / "isbns.txt"
STATE_FILE = BASE_DIR / "state.json"
CACHE_FILE = BASE_DIR / "isbn_cache.json"  # ISBN → 교보 saleCmdtid 캐시

# ─── 공통 헤더 ────────────────────────────────────────────────
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

JSON_HEADERS = {**HTML_HEADERS, "Accept": "application/json"}


# ─── 설정 / 상태 로딩 ─────────────────────────────────────────
def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)


def load_isbns() -> list:
    lines = ISBNS_FILE.read_text().splitlines()
    valid = []
    for line in lines:
        isbn = line.strip()
        if not isbn:
            continue
        if len(isbn) != 13 or not isbn.isdigit():
            print(f"[SKIP] 잘못된 ISBN 형식: {isbn}")
            continue
        valid.append(isbn)
    return valid


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_cache() -> dict:
    if CACHE_FILE.exists():
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}


def save_cache(cache: dict):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# ─── 알라딘 ───────────────────────────────────────────────────
def get_aladin_rating(isbn: str, session: requests.Session) -> tuple:
    """알라딘 평점 조회. (rating, title) 반환"""
    url = f"https://www.aladin.co.kr/shop/wproduct.aspx?ISBN={isbn}"
    try:
        r = session.get(url, headers=HTML_HEADERS, timeout=15)
        if r.status_code != 200:
            return None, ""
        html = r.text
        m = re.search(r'"ratingValue":\s*"?([0-9]+(?:\.[0-9]+)?)"?', html)
        rating = float(m.group(1)) if m else None
        t = re.search(r'<meta property="og:title" content="([^"]+)"', html)
        title = t.group(1).strip() if t else isbn
        return rating, title
    except Exception as e:
        print(f"    [알라딘 오류] {e}")
        return None, ""


# ─── 예스24 ───────────────────────────────────────────────────
def init_yes24_session() -> requests.Session:
    """Yes24 세션 초기화 (쿠키 획득)"""
    session = requests.Session()
    try:
        session.get("https://www.yes24.com/", headers=HTML_HEADERS, timeout=15)
    except Exception:
        pass
    return session


def get_yes24_rating(isbn: str, session: requests.Session) -> tuple:
    """예스24 검색 결과에서 평점 조회"""
    url = f"https://www.yes24.com/Product/Search?query={isbn}&domain=BOOK"
    try:
        r = session.get(url, headers=HTML_HEADERS, timeout=15)
        if r.status_code != 200:
            return None, ""
        html = r.text
        m = re.search(r'rating_grade.*?yes_b">([0-9]+(?:\.[0-9]+)?)', html, re.DOTALL)
        rating = float(m.group(1)) if m else None
        t = re.search(r'class="gd_name"[^>]*>([^<]+)', html)
        title = t.group(1).strip() if t else isbn
        return rating, title
    except Exception as e:
        print(f"    [예스24 오류] {e}")
        return None, ""


# ─── 교보문고 ─────────────────────────────────────────────────
def get_kyobo_product_id(isbn: str, cache: dict, session: requests.Session) -> str:
    """ISBN → 교보문고 saleCmdtid 변환 (캐시 활용)"""
    if isbn in cache:
        return cache[isbn]

    url = f"https://search.kyobobook.co.kr/search?keyword={isbn}&gbCode=TOT&target=total"
    try:
        r = session.get(url, headers={**HTML_HEADERS, "Referer": "https://www.kyobobook.co.kr/"}, timeout=15)
        html = r.text
        # data-bid="{isbn}" 인 항목의 data-pid 추출
        m = re.search(rf'data-pid="(S\d+)"[^>]*data-bid="{isbn}"', html)
        if not m:
            m = re.search(rf'data-bid="{isbn}"[^>]*data-pid="(S\d+)"', html)
        if m:
            product_id = m.group(1)
            cache[isbn] = product_id
            return product_id
    except Exception as e:
        print(f"    [교보 검색 오류] {e}")
    return None


def get_kyobo_rating(isbn: str, cache: dict, session: requests.Session) -> tuple:
    """교보문고 평점 조회"""
    product_id = get_kyobo_product_id(isbn, cache, session)
    if not product_id:
        return None, ""

    url = f"https://product.kyobobook.co.kr/api/review/statistics?saleCmdtid={product_id}"
    try:
        r = session.get(
            url,
            headers={**JSON_HEADERS, "Referer": f"https://product.kyobobook.co.kr/detail/{product_id}"},
            timeout=15,
        )
        data = r.json()
        inner = data.get("data") or {}
        avg = inner.get("revwRvgrAvg")
        rating = float(avg) if avg is not None else None

        # 교보 상품 페이지에서 제목 가져오기 (캐시에 없을 때만)
        title_key = f"_title_{isbn}"
        if title_key not in cache:
            try:
                rp = session.get(
                    f"https://product.kyobobook.co.kr/detail/{product_id}",
                    headers=HTML_HEADERS, timeout=15
                )
                tm = re.search(r'<meta property="og:title" content="([^"]+)"', rp.text)
                cache[title_key] = tm.group(1).strip() if tm else isbn
            except Exception:
                cache[title_key] = isbn

        return rating, cache[title_key]
    except Exception as e:
        print(f"    [교보 평점 오류] {e}")
        return None, ""


# ─── 알림 ─────────────────────────────────────────────────────
def send_slack(webhook_url: str, messages: list):
    if not webhook_url:
        return
    text = "\n\n".join(messages)
    try:
        requests.post(webhook_url, json={"text": text}, timeout=10)
        print("[Slack 알림 발송 완료]")
    except Exception as e:
        print(f"[Slack 알림 실패] {e}")


def send_email(config: dict, messages: list):
    ec = config.get("email", {})
    if not ec.get("enabled") or not ec.get("username"):
        return
    try:
        body = "\n\n".join(messages)
        msg = MIMEMultipart()
        msg["From"] = ec["username"]
        msg["To"] = ec["to"]
        msg["Subject"] = f"[도서 평점 알림] {len(messages)}건 평점 하락 감지"
        msg.attach(MIMEText(body, "plain", "utf-8"))
        with smtplib.SMTP(ec["smtp_server"], ec["smtp_port"]) as server:
            server.starttls()
            server.login(ec["username"], ec["password"])
            server.send_message(msg)
        print("[이메일 알림 발송 완료]")
    except Exception as e:
        print(f"[이메일 알림 실패] {e}")


# ─── 메인 ─────────────────────────────────────────────────────
def run():
    # --init 옵션: 베이스라인만 저장하고 알림 없이 종료
    init_mode = "--init" in sys.argv

    config = load_config()
    threshold = config["threshold"]
    isbns = load_isbns()
    state = load_state()
    cache = load_cache()
    stores = config.get("stores", ["aladin", "yes24", "kyobo"])
    delay = config.get("request_delay_seconds", 0.5)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode_label = " [초기화 모드 - 알림 없이 베이스라인 저장]" if init_mode else ""
    print(f"\n{'='*60}")
    print(f"도서 평점 모니터 실행: {now}{mode_label}")
    print(f"대상 도서: {len(isbns)}권 | 임계값: {threshold} | 서점: {', '.join(stores)}")
    print(f"{'='*60}\n")

    # 세션 초기화
    aladin_session = requests.Session()
    yes24_session = init_yes24_session()
    kyobo_session = requests.Session()

    alerts = []

    for i, isbn in enumerate(isbns, 1):
        print(f"[{i}/{len(isbns)}] ISBN {isbn}")
        isbn_state = state.setdefault(isbn, {})

        scrapers = {
            "aladin": lambda isbn=isbn: get_aladin_rating(isbn, aladin_session),
            "yes24": lambda isbn=isbn: get_yes24_rating(isbn, yes24_session),
            "kyobo": lambda isbn=isbn: get_kyobo_rating(isbn, cache, kyobo_session),
        }

        store_urls = {
            "aladin": f"https://www.aladin.co.kr/shop/wproduct.aspx?ISBN={isbn}",
            "yes24": f"https://www.yes24.com/Product/Search?query={isbn}&domain=BOOK",
            "kyobo": f"https://product.kyobobook.co.kr/detail/{cache.get(isbn, isbn)}",
        }

        for store in stores:
            rating, title = scrapers[store]()
            time.sleep(delay)

            prev = isbn_state.get(store)

            if rating is None:
                print(f"  {store:8s} → 평점 없음")
                continue

            print(f"  {store:8s} → {rating:.1f}  (이전: {prev if prev is not None else '없음'})")
            isbn_state[store] = rating

            # 초기화 모드에서는 알림 없이 저장만
            if init_mode:
                continue

            # 알림 조건: threshold 아래이고, 이전보다 낮거나 최초 확인
            if rating < threshold:
                if prev is None or rating < prev:
                    book_name = title if title and title != isbn else isbn
                    # 교보 URL은 캐시가 업데이트된 후 반영
                    url = store_urls["kyobo"] if store == "kyobo" else store_urls[store]
                    if store == "kyobo" and isbn in cache:
                        url = f"https://product.kyobobook.co.kr/detail/{cache[isbn]}"
                    msg = (
                        f"⚠️ 평점 하락 감지!\n"
                        f"  서점: {store}\n"
                        f"  도서: {book_name}\n"
                        f"  ISBN: {isbn}\n"
                        f"  현재 평점: {rating:.1f} (이전: {f'{prev:.1f}' if prev is not None else '최초 확인'}) / 임계값: {threshold}\n"
                        f"  URL: {url}"
                    )
                    alerts.append(msg)
                    print(f"  *** 알림 대상 ***")

        state[isbn] = isbn_state

    # 상태 및 캐시 저장
    save_state(state)
    save_cache(cache)

    if init_mode:
        print(f"\n{'='*60}")
        print("초기화 완료. 이제 'python3 monitor.py' 로 모니터링을 시작하세요.")
        print(f"{'='*60}\n")
        return

    # 알림 출력 및 발송
    print(f"\n{'='*60}")
    if alerts:
        print(f"알림 {len(alerts)}건 발생:\n")
        for msg in alerts:
            print(msg)
            print()
        notif = config.get("notification", {})
        send_slack(notif.get("slack_webhook", ""), alerts)
        send_email(notif, alerts)
    else:
        print("임계값 아래의 신규 평점 하락 없음.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    run()

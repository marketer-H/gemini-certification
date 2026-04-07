#!/usr/bin/env python3
"""
도서 평점 모니터: 교보문고 / 예스24 / 알라딘
평점이 threshold 아래로 내려가면 알림 발송
"""

import json
import os
import re
import sys
import smtplib
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

import requests

warnings.filterwarnings("ignore")

# ─── 파일 경로 ────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
ISBNS_FILE  = BASE_DIR / "isbns.txt"
STATE_FILE  = BASE_DIR / "state.json"
CACHE_FILE  = BASE_DIR / "isbn_cache.json"

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
    valid = []
    for line in ISBNS_FILE.read_text().splitlines():
        isbn = line.strip()
        if not isbn:
            continue
        if len(isbn) != 13 or not isbn.isdigit():
            print(f"[SKIP] 잘못된 ISBN: {isbn}")
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
def get_aladin_rating(isbn: str) -> tuple:
    url = f"https://www.aladin.co.kr/shop/wproduct.aspx?ISBN={isbn}"
    try:
        r = requests.get(url, headers=HTML_HEADERS, timeout=15)
        if r.status_code != 200:
            return None, ""
        m = re.search(r'"ratingValue":\s*"?([0-9]+(?:\.[0-9]+)?)"?', r.text)
        rating = float(m.group(1)) if m else None
        t = re.search(r'<meta property="og:title" content="([^"]+)"', r.text)
        title = t.group(1).strip() if t else ""
        return rating, title
    except Exception:
        return None, ""


# ─── 예스24 ───────────────────────────────────────────────────
_yes24_lock = threading.Lock()  # Yes24는 동시 요청 차단 → 순차 처리

def get_yes24_rating(isbn: str, session: requests.Session) -> tuple:
    url = f"https://www.yes24.com/Product/Search?query={isbn}&domain=BOOK"
    with _yes24_lock:
        try:
            r = session.get(url, headers=HTML_HEADERS, timeout=15)
            if r.status_code != 200:
                return None, ""
            m = re.search(r'rating_grade.*?yes_b">([0-9]+(?:\.[0-9]+)?)', r.text, re.DOTALL)
            rating = float(m.group(1)) if m else None
            t = re.search(r'class="gd_name"[^>]*>([^<]+)', r.text)
            title = t.group(1).strip() if t else ""
            return rating, title
        except Exception:
            return None, ""


# ─── 교보문고 ─────────────────────────────────────────────────
def get_kyobo_rating(isbn: str, cache: dict, cache_lock: threading.Lock) -> tuple:
    # product ID 조회 (캐시 우선)
    with cache_lock:
        product_id = cache.get(isbn)

    if not product_id:
        url = f"https://search.kyobobook.co.kr/search?keyword={isbn}&gbCode=TOT&target=total"
        try:
            r = requests.get(url, headers={**HTML_HEADERS, "Referer": "https://www.kyobobook.co.kr/"}, timeout=15)
            m = re.search(rf'data-pid="(S\d+)"[^>]*data-bid="{isbn}"', r.text)
            if not m:
                m = re.search(rf'data-bid="{isbn}"[^>]*data-pid="(S\d+)"', r.text)
            if m:
                product_id = m.group(1)
                with cache_lock:
                    cache[isbn] = product_id
        except Exception:
            return None, ""

    if not product_id:
        return None, ""

    # 평점 조회
    try:
        r = requests.get(
            f"https://product.kyobobook.co.kr/api/review/statistics?saleCmdtid={product_id}",
            headers={**JSON_HEADERS, "Referer": f"https://product.kyobobook.co.kr/detail/{product_id}"},
            timeout=15,
        )
        inner = r.json().get("data") or {}
        avg = inner.get("revwRvgrAvg")
        rating = float(avg) if avg is not None else None
    except Exception:
        return None, ""

    # 제목 조회 (캐시 우선)
    title_key = f"_title_{isbn}"
    with cache_lock:
        title = cache.get(title_key, "")

    if not title:
        try:
            rp = requests.get(
                f"https://product.kyobobook.co.kr/detail/{product_id}",
                headers=HTML_HEADERS, timeout=15
            )
            tm = re.search(r'<meta property="og:title" content="([^"]+)"', rp.text)
            title = tm.group(1).strip() if tm else ""
            with cache_lock:
                cache[title_key] = title
        except Exception:
            pass

    return rating, title


# ─── ISBN 1개 처리 ────────────────────────────────────────────
def process_isbn(isbn: str, stores: list, yes24_session: requests.Session,
                 cache: dict, cache_lock: threading.Lock) -> dict:
    """한 ISBN의 모든 서점 평점을 조회. {store: (rating, title)} 반환"""
    results = {}
    for store in stores:
        if store == "aladin":
            results[store] = get_aladin_rating(isbn)
        elif store == "yes24":
            results[store] = get_yes24_rating(isbn, yes24_session)
        elif store == "kyobo":
            results[store] = get_kyobo_rating(isbn, cache, cache_lock)
    return results


# ─── 알림 ─────────────────────────────────────────────────────
def send_slack(webhook_url: str, messages: list):
    if not webhook_url:
        return
    try:
        requests.post(webhook_url, json={"text": "\n\n".join(messages)}, timeout=10)
        print("[Slack 알림 발송 완료]")
    except Exception as e:
        print(f"[Slack 알림 실패] {e}")


def send_email(config: dict, messages: list):
    from email.message import EmailMessage
    ec = config.get("email", {})
    if not ec.get("enabled") or not ec.get("username"):
        return
    password = ec.get("password") or os.environ.get("SMTP_PASSWORD", "")
    if not password:
        print("[이메일 알림 실패] 비밀번호 없음. config.json 또는 SMTP_PASSWORD 환경변수를 설정하세요.")
        return
    try:
        # 본문에서 non-breaking space 등 특수 공백 정리
        body = "\n\n".join(messages).replace("\xa0", " ")
        msg = EmailMessage()
        msg["From"] = ec["username"]
        msg["To"] = ec["to"]
        is_report = len(messages) == 1 and "미만 도서" in messages[0]
        if is_report:
            msg["Subject"] = "[도서 평점 리포트] 현재 평점 미만 도서 현황"
        else:
            msg["Subject"] = f"[도서 평점 알림] {len(messages)}건 평점 하락 감지"
        msg.set_content(body, charset="utf-8")
        with smtplib.SMTP(ec["smtp_server"], ec["smtp_port"]) as server:
            server.starttls()
            server.login(ec["username"], password)
            server.send_message(msg)
        print("[이메일 알림 발송 완료]")
    except Exception as e:
        print(f"[이메일 알림 실패] {e}")


# ─── 메인 ─────────────────────────────────────────────────────
def clean_title(title: str, isbn: str) -> str:
    """도서명에서 불필요한 접미사 제거"""
    if not title or title == isbn:
        return isbn
    # " | ..." 이후 제거 (알라딘: "제목 | 시리즈 | 저자")
    title = title.split(" | ")[0].strip()
    # " - 교보문고" 등 제거
    for suffix in [" - 교보문고", " - 예스24", " - 알라딘"]:
        if title.endswith(suffix):
            title = title[:-len(suffix)].strip()
    return title


def report():
    """state.json 기반으로 현재 9.5 아래인 도서 목록 출력"""
    config    = load_config()
    threshold = config["threshold"]
    state     = load_state()
    cache     = load_cache()
    stores    = config.get("stores", ["aladin", "yes24", "kyobo"])

    rows = []
    for isbn, isbn_state in state.items():
        for store in stores:
            rating = isbn_state.get(store)
            if rating is not None and rating < threshold:
                title_key = f"_title_{isbn}"
                raw_title = cache.get(title_key) or isbn
                title = clean_title(raw_title, isbn)
                rows.append((rating, store, isbn, title))

    if not rows:
        print(f"\n임계값({threshold}) 아래인 도서 없음.\n")
        return

    rows.sort(key=lambda x: x[0])  # 낮은 평점 순

    # 콘솔 출력
    print(f"\n{'='*60}")
    print(f"현재 평점 {threshold} 미만 도서 ({len(rows)}건)")
    print(f"{'='*60}")
    print(f"{'평점':>5}  {'서점':<8}  {'ISBN':<15}  도서명")
    print(f"{'-'*60}")
    for rating, store, isbn, title in rows:
        print(f"{rating:>5.1f}  {store:<8}  {isbn:<15}  {title}")
    print(f"{'='*60}\n")

    # 이메일 발송
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"[{now}] 현재 평점 {threshold} 미만 도서 ({len(rows)}건)\n"]
    for rating, store, isbn, title in rows:
        lines.append(f"{rating:.1f}  {store:<8}  {isbn}  {title}")
    send_email(config.get("notification", {}), ["\n".join(lines)])


def run():
    init_mode = "--init" in sys.argv

    config    = load_config()
    threshold = config["threshold"]
    isbns     = load_isbns()
    state     = load_state()
    cache     = load_cache()
    stores    = config.get("stores", ["aladin", "yes24", "kyobo"])
    workers   = config.get("workers", 8)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode_label = " [초기화 모드]" if init_mode else ""
    print(f"\n{'='*60}")
    print(f"도서 평점 모니터 실행: {now}{mode_label}")
    print(f"대상 도서: {len(isbns)}권 | 임계값: {threshold} | 병렬: {workers}개")
    print(f"{'='*60}\n")

    # Yes24 세션 1회 초기화 (쿠키 획득)
    yes24_session = requests.Session()
    try:
        yes24_session.get("https://www.yes24.com/", headers=HTML_HEADERS, timeout=15)
    except Exception:
        pass

    cache_lock  = threading.Lock()
    print_lock  = threading.Lock()
    state_lock  = threading.Lock()
    alerts      = []
    alerts_lock = threading.Lock()
    done        = [0]

    def handle_isbn(isbn: str):
        results = process_isbn(isbn, stores, yes24_session, cache, cache_lock)

        # 알라딘에서 가져온 제목을 다른 서점 알림에도 공유
        shared_title = ""
        for store in stores:
            _, t = results.get(store, (None, ""))
            if t and t != isbn:
                shared_title = t
                break

        isbn_alerts = []
        with state_lock:
            isbn_state = state.setdefault(isbn, {})

        for store in stores:
            rating, title = results.get(store, (None, ""))
            book_name = clean_title(title or shared_title, isbn)

            with state_lock:
                prev = isbn_state.get(store)
                isbn_state[store] = rating if rating is not None else prev

            if rating is None:
                continue

            if not init_mode and rating < threshold and (prev is None or rating < prev):
                store_url = {
                    "aladin": f"https://www.aladin.co.kr/shop/wproduct.aspx?ISBN={isbn}",
                    "yes24":  f"https://www.yes24.com/Product/Search?query={isbn}&domain=BOOK",
                    "kyobo":  f"https://product.kyobobook.co.kr/detail/{cache.get(isbn, isbn)}",
                }[store]
                isbn_alerts.append(
                    f"⚠️ 평점 하락 감지!\n"
                    f"  서점: {store}\n"
                    f"  도서: {book_name} (ISBN: {isbn})\n"
                    f"  현재 평점: {rating:.1f} (이전: {f'{prev:.1f}' if prev is not None else '최초 확인'}) / 임계값: {threshold}\n"
                    f"  URL: {store_url}"
                )

        with state_lock:
            state[isbn] = isbn_state
            done[0] += 1
            n = done[0]

        rating_str = " | ".join(
            f"{s}: {results[s][0]:.1f}" if results.get(s) and results[s][0] is not None else f"{s}: -"
            for s in stores
        )
        alert_mark = " *** 알림 ***" if isbn_alerts else ""
        with print_lock:
            print(f"[{n}/{len(isbns)}] {isbn}  {rating_str}{alert_mark}")

        if isbn_alerts:
            with alerts_lock:
                alerts.extend(isbn_alerts)

    # 병렬 실행
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(handle_isbn, isbn): isbn for isbn in isbns}
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                with print_lock:
                    print(f"  [오류] {futures[f]}: {e}")

    save_state(state)
    save_cache(cache)

    print(f"\n{'='*60}")
    if init_mode:
        print("초기화 완료. 이제 'python3 monitor.py' 로 모니터링을 시작하세요.")
    elif alerts:
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
    if "--report" in sys.argv:
        report()
    else:
        run()

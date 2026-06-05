#!/usr/bin/env python3
"""
이지스퍼블리싱 구매평 모니터 → Discord 알림봇

사용법:
  python review_bot.py            # 전체 실행
  python review_bot.py --init     # 첫 실행: 현재 리뷰를 기준점으로 저장 (알림 없음)
  python review_bot.py --test     # Discord 웹훅 연결 테스트
  python review_bot.py --days 365 # 최근 N일 이내 출판 도서만 확인 (기본 730일)

config.json에 "discord_webhook" 필드를 추가하세요:
  "discord_webhook": "https://discord.com/api/webhooks/..."
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_module
import json
import re
import time
import warnings
from datetime import datetime, date
from pathlib import Path

import requests

warnings.filterwarnings("ignore")

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
ISBNS_FILE = BASE_DIR / "isbns.txt"
REVIEW_STATE_FILE = BASE_DIR / "review_state.json"
CACHE_FILE = BASE_DIR / "isbn_cache.json"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HTML_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Accept-Encoding": "identity",
}
JSON_HEADERS = {**HTML_HEADERS, "Accept": "application/json, text/plain, */*"}
AJAX_HEADERS = {**HTML_HEADERS, "Accept": "*/*", "X-Requested-With": "XMLHttpRequest"}

STORE_NAMES = {"aladin": "알라딘", "yes24": "예스24", "kyobo": "교보문고"}
STORE_COLORS = {"aladin": 0xEF4128, "yes24": 0xFF6B00, "kyobo": 0x009261}
STORE_EMOJI = {"aladin": "🔴", "yes24": "🟠", "kyobo": "🟢"}


# ─── 설정 / 상태 ─────────────────────────────────────────────────────
def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)

def load_isbns(max_days: int = 730) -> list:
    """(isbn, pub_date) 리스트 반환. max_days 이내 출판 도서만."""
    cutoff = date.today().toordinal() - max_days
    result = []
    for line in ISBNS_FILE.read_text().splitlines():
        parts = line.split()
        if not parts:
            continue
        isbn = parts[0].strip()
        if len(isbn) != 13 or not isbn.isdigit():
            continue
        pub_str = parts[1].strip() if len(parts) > 1 else "2099-01-01"
        try:
            d = date.fromisoformat(pub_str)
            if d.toordinal() >= cutoff:
                result.append((isbn, pub_str))
        except ValueError:
            result.append((isbn, "unknown"))
    return result

def load_state() -> dict:
    if REVIEW_STATE_FILE.exists():
        with open(REVIEW_STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state: dict):
    with open(REVIEW_STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def load_cache() -> dict:
    if CACHE_FILE.exists():
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}

def save_cache(cache: dict):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

def text_hash(reviewer: str, text: str) -> str:
    key = f"{reviewer}:{text[:80]}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:14]

def clean_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html_module.unescape(text)
    text = re.sub(r"회색\s*영역을\s*클릭하면\s*내용을\s*확인할\s*수\s*있습니다\.", "", text)
    text = re.sub(r"이\s*글에는\s*스포일러가\s*포함되어\s*있습니다\.\s*보시겠습니까\?", "", text)
    return re.sub(r"\s+", " ", text).strip()


# ─── 알라딘 ──────────────────────────────────────────────────────────
def get_aladin_reviews(isbn: str, cache: dict) -> list:
    title_key = f"_aladin_title_{isbn}"
    item_key = f"_aladin_item_{isbn}"

    try:
        # 상품 페이지 → ItemId + 제목
        if not cache.get(item_key) or not cache.get(title_key):
            r = requests.get(
                f"https://www.aladin.co.kr/shop/wproduct.aspx?ISBN={isbn}",
                headers=HTML_HEADERS, timeout=15,
            )
            if r.status_code != 200:
                return []
            # ItemId: URL 또는 페이지 내 모든 매치 중 0이 아닌 첫번째
            all_ids = re.findall(r'ItemId=(\d+)', r.url + r.text)
            item_id = next((x for x in all_ids if x != "0"), None)
            if item_id:
                cache[item_key] = item_id
            if not cache.get(title_key):
                tm = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', r.text)
                cache[title_key] = tm.group(1).strip() if tm else isbn

        item_id = cache.get(item_key)
        title = cache.get(title_key, isbn)
        if not item_id:
            return []

        referer = f"https://www.aladin.co.kr/shop/wproduct.aspx?ItemId={item_id}"
        results = []

        # 구매평 (CommentReview)
        r1 = requests.get(
            f"https://www.aladin.co.kr/ucl/shop/product/ajax/GetCommunityListAjax.aspx"
            f"?ProductItemId={item_id}&itemId={item_id}&pageCount=10"
            f"&communitytype=CommentReview&nemoType=-1&page=1"
            f"&startNumber=1&endNumber=10&sort=2&IsOrderer=2&BranchType=1"
            f"&IsAjax=true&pageType=0",
            headers={**AJAX_HEADERS, "Referer": referer}, timeout=15,
        )
        if r1.status_code == 200 and "없습니다" not in r1.text:
            results.extend(_parse_aladin_comment_reviews(r1.text, title, item_id))

        # 일반 리뷰 (MyReview)
        r2 = requests.get(
            f"https://www.aladin.co.kr/ucl/shop/product/ajax/GetCommunityListAjax.aspx"
            f"?ProductItemId={item_id}&itemId={item_id}&pageCount=10"
            f"&communitytype=MyReview&nemoType=-1&page=1"
            f"&startNumber=1&endNumber=10&sort=2&IsOrderer=2&BranchType=1"
            f"&IsAjax=true&pageType=0",
            headers={**AJAX_HEADERS, "Referer": referer}, timeout=15,
        )
        if r2.status_code == 200 and "없습니다" not in r2.text:
            results.extend(_parse_aladin_my_reviews(r2.text, title, item_id))

        return results

    except Exception as e:
        print(f"    [알라딘 오류] {isbn}: {e}")
        return []


def _parse_aladin_comment_reviews(html: str, title: str, item_id: str) -> list:
    reviews = []
    # hundred_list 블록 단위로 리뷰 분리
    blocks = re.split(r'<div class="hundred_list">', html)

    for block in blocks[1:]:
        # 리뷰 ID (commentReviewPaper{N})
        id_m = re.search(r'commentReviewPaper(\d+)', block)
        rv_id = id_m.group(1) if id_m else None
        if not rv_id:
            continue

        # 별점: star_on 이미지 개수
        stars = len(re.findall(r"icon_star_on\.png", block))

        # 리뷰어명 + 날짜
        reviewer_m = re.search(
            r'href="[^"]*UserReview[^"]*"[^>]*>([^<]+)</a>', block
        )
        if not reviewer_m:
            # 대안: 날짜 앞에 있는 ID
            reviewer_m = re.search(r'<a[^>]+class="lnk_id"[^>]*>([^<]+)</a>', block)
        reviewer = reviewer_m.group(1).strip() if reviewer_m else "익명"

        # 리뷰 텍스트
        text_m = re.search(
            rf'id="div_commentReviewPaper{rv_id}"[^>]*>(.*?)</div>',
            block, re.DOTALL,
        )
        if not text_m:
            # 스포일러 없는 경우
            text_m = re.search(r'<div[^>]*id="[^"]*Paper[^"]*"[^>]*>(.*?)</div>', block, re.DOTALL)
        text = clean_text(text_m.group(1)) if text_m else ""

        if text and len(text) > 5:
            reviews.append({
                "id": rv_id,
                "reviewer": reviewer,
                "rating": str(stars),
                "text": text,
                "title": title,
                "link": f"https://www.aladin.co.kr/shop/wproduct.aspx?ItemId={item_id}#coReview",
            })

    return reviews


def _parse_aladin_my_reviews(html: str, title: str, item_id: str) -> list:
    """알라딘 일반 리뷰(MyReview) 파싱"""
    reviews = []
    blocks = re.split(r'<div class="hundred_list">', html)

    for block in blocks[1:]:
        # 리뷰 ID (fn_toggle_mypaper에서)
        id_m = re.search(r"fn_toggle_mypaper\((\d+)", block)
        rv_id = id_m.group(1) if id_m else None
        if not rv_id:
            continue

        stars = len(re.findall(r"icon_star_on\.png", block))

        # 리뷰 제목 (Ere_str)
        title_m = re.search(r'class="Ere_str">([^<]+)', block)
        rv_title = title_m.group(1).strip() if title_m else ""

        # 리뷰어명
        reviewer_m = re.search(r'blog\.aladin\.co\.kr/(\d+)/', block)
        reviewer = reviewer_m.group(1) if reviewer_m else "익명"

        # 본문 (paperShort_{id})
        text_m = re.search(rf'id="paperShort_{rv_id}"[^>]*>(.*?)</div>', block, re.DOTALL)
        text = clean_text(text_m.group(1)) if text_m else rv_title

        if text and len(text) > 5:
            reviews.append({
                "id": f"M{rv_id}",  # CommentReview ID와 구분
                "reviewer": reviewer,
                "rating": str(stars),
                "text": f"[일반 리뷰] {rv_title}\n{text}" if rv_title and rv_title not in text else text,
                "title": title,
                "link": f"https://www.aladin.co.kr/shop/wproduct.aspx?ItemId={item_id}#myReview",
            })

    return reviews


# ─── 예스24 ──────────────────────────────────────────────────────────
_yes24_session = requests.Session()
_yes24_session.headers.update(HTML_HEADERS)

def yes24_batch_find_ids(isbns: list, cache: dict):
    """Playwright로 Yes24 홈 세션을 맺은 뒤 ISBN 목록의 product ID를 일괄 캐싱"""
    missing = [isbn for isbn in isbns if not cache.get(f"_yes24_pid_{isbn}")]
    if not missing:
        return
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=_UA,
                locale="ko-KR",
                timezone_id="Asia/Seoul",
            )
            page = ctx.new_page()
            page.goto("https://www.yes24.com/", timeout=15000)
            page.wait_for_load_state("networkidle", timeout=8000)

            for isbn in missing:
                try:
                    page.goto(
                        f"https://www.yes24.com/Product/Search?query={isbn}&domain=BOOK",
                        timeout=15000,
                    )
                    page.wait_for_load_state("networkidle", timeout=8000)
                    html = page.content()
                    # onclick에 ISBN이 포함된 패턴에서 product ID 추출
                    # 예: setGoodsClickExtraCodeHub('029','9791163037873','163301895','0',this)
                    m = re.search(
                        rf"setGoodsClickExtraCodeHub\('[^']*',\s*'{re.escape(isbn)}',\s*'(\d+)'",
                        html,
                    )
                    if not m:
                        # 이미지 URL 패턴: image.yes24.com/goods/{id}/L
                        # 이미지 alt에 제목이 있는 경우 근처에서 ID 추출
                        m = re.search(
                            r"image\.yes24\.com/goods/(\d+)/[LMS]",
                            html[max(0, html.find(isbn)-500):html.find(isbn)+500] if isbn in html else "",
                        )
                    if m:
                        pid = m.group(1)
                        # 제목은 알라딘 캐시 우선, 없으면 검색결과에서 추출
                        title = (
                            cache.get(f"_aladin_title_{isbn}")
                            or cache.get(f"_title_{isbn}")
                            or isbn
                        )
                        cache[f"_yes24_pid_{isbn}"] = pid
                        cache[f"_yes24_title_{isbn}"] = title
                        print(f"    [예스24] {isbn} → {pid} ({title[:30]})")
                except Exception:
                    pass
                time.sleep(0.5)
            browser.close()
    except Exception as e:
        print(f"    [예스24 Playwright 오류] {e}")


def _yes24_product_id(isbn: str, cache: dict) -> tuple:
    """(product_id, title) 반환"""
    id_key = f"_yes24_pid_{isbn}"
    title_key = f"_yes24_title_{isbn}"
    return cache.get(id_key), cache.get(title_key, isbn)


def get_yes24_reviews(isbn: str, cache: dict) -> list:
    pid, title = _yes24_product_id(isbn, cache)
    if not pid:
        return []
    try:
        url = (
            f"https://www.yes24.com/Product/communityModules/GoodsReviewList/{pid}"
            f"?goodsSetYn=N&Sort=1&PageNumber=1&Type=ALL"
        )
        r = _yes24_session.get(url, headers={
            "Accept": "text/html,*/*",
            "Referer": f"https://www.yes24.com/Product/Goods/{pid}",
        }, timeout=15)
        if r.status_code != 200:
            return []
        return _parse_yes24_reviews(r.text, title, pid)
    except Exception as e:
        print(f"    [예스24 오류] {isbn}: {e}")
        return []


def _parse_yes24_reviews(html: str, title: str, pid: str) -> list:
    reviews = []
    blocks = re.split(r'<div class="reviewInfoGrp', html)

    for block in blocks[1:]:
        # 리뷰 ID (OpenReviewReport 에서)
        id_m = re.search(r"OpenReviewReport\((\d+)\)", block)
        rv_id = id_m.group(1) if id_m else None
        if not rv_id:
            continue

        # 별점 (10점 기준)
        rating_m = re.search(r"total_rating_(\d+)", block)
        rating = rating_m.group(1) if rating_m else ""

        # 리뷰어
        reviewer_m = re.search(
            r'class="lnk_id">([^<]+)</a>', block
        )
        reviewer = reviewer_m.group(1).strip() if reviewer_m else "익명"

        # 날짜
        date_m = re.search(r'txt_date">([^<]+)</em>', block)
        date_str = date_m.group(1).strip() if date_m else ""

        # 리뷰 텍스트 (review_cont div)
        text_m = re.search(r'class="review_cont"[^>]*>(.*?)</div>', block, re.DOTALL)
        if not text_m:
            text_m = re.search(r'reviewInfoBot[^>]*>.*?<div[^>]*>(.*?)</div>', block, re.DOTALL)
        text = clean_text(text_m.group(1)) if text_m else ""

        if text and len(text) > 5:
            reviews.append({
                "id": rv_id,
                "reviewer": reviewer,
                "rating": f"{int(rating)//2}점/5점" if rating else "",
                "text": text,
                "title": title,
                "link": f"https://www.yes24.com/Product/Goods/{pid}#review",
            })

    return reviews


# ─── 교보문고 ─────────────────────────────────────────────────────────
def _kyobo_product_id(isbn: str, cache: dict) -> str | None:
    if cache.get(isbn):
        return cache[isbn]
    try:
        r = requests.get(
            f"https://search.kyobobook.co.kr/search?keyword={isbn}&gbCode=TOT&target=total",
            headers={**HTML_HEADERS, "Referer": "https://www.kyobobook.co.kr/"},
            timeout=15,
        )
        m = re.search(rf'data-pid="(S\d+)"[^>]*data-bid="{isbn}"', r.text)
        if not m:
            m = re.search(rf'data-bid="{isbn}"[^>]*data-pid="(S\d+)"', r.text)
        if m:
            cache[isbn] = m.group(1)
            return cache[isbn]
    except Exception:
        pass
    return None


def get_kyobo_review_count(isbn: str, cache: dict) -> tuple:
    """(product_id, review_count, title) 반환. 리뷰 목록 API 불가 → count 기반 감지."""
    product_id = _kyobo_product_id(isbn, cache)
    if not product_id:
        return None, 0, isbn

    title_key = f"_title_{isbn}"
    title = cache.get(title_key, "")
    if not title:
        try:
            rp = requests.get(
                f"https://product.kyobobook.co.kr/detail/{product_id}",
                headers=HTML_HEADERS, timeout=15,
            )
            tm = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', rp.text)
            title = tm.group(1).strip() if tm else isbn
            cache[title_key] = title
        except Exception:
            title = isbn

    try:
        r = requests.get(
            f"https://product.kyobobook.co.kr/api/review/statistics?saleCmdtid={product_id}",
            headers={**JSON_HEADERS, "Referer": f"https://product.kyobobook.co.kr/detail/{product_id}"},
            timeout=15,
        )
        data = r.json().get("data") or {}
        count = int(data.get("whlRevwCont") or 0)
        return product_id, count, title
    except Exception as e:
        print(f"    [교보 오류] {isbn}: {e}")
        return product_id, 0, title


# ─── Discord 발송 ─────────────────────────────────────────────────────
def send_discord(webhook_url: str, new_reviews: list):
    if not webhook_url:
        print("[Discord] config.json에 discord_webhook URL이 없습니다.")
        return
    if not new_reviews:
        return

    embeds = []
    for rv in new_reviews:
        store = rv["store"]
        text = rv.get("text", "")
        if len(text) > 350:
            text = text[:347] + "..."

        embed = {
            "title": f"{STORE_EMOJI.get(store, '📚')} 새 구매평 | {rv['title']}",
            "url": rv.get("link", ""),
            "description": text or "(내용 없음)",
            "color": STORE_COLORS.get(store, 0x7289DA),
            "fields": [
                {"name": "서점", "value": STORE_NAMES.get(store, store), "inline": True},
            ],
            "footer": {"text": f"리뷰어: {rv.get('reviewer', '?')}"},
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        if rv.get("rating"):
            embed["fields"].append({"name": "별점", "value": f"⭐ {rv['rating']}", "inline": True})
        embeds.append(embed)

    for i in range(0, len(embeds), 10):
        batch = embeds[i:i + 10]
        try:
            r = requests.post(webhook_url, json={"embeds": batch}, timeout=10)
            if r.status_code not in (200, 204):
                print(f"[Discord] 발송 실패: HTTP {r.status_code} — {r.text[:100]}")
            else:
                print(f"[Discord] {len(batch)}개 알림 발송 완료")
        except Exception as e:
            print(f"[Discord] 오류: {e}")
        if i + 10 < len(embeds):
            time.sleep(1)


# ─── 메인 ────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="이지스퍼블리싱 구매평 Discord 알림봇")
    parser.add_argument("--init", action="store_true", help="첫 실행: 현재 리뷰 기준점 저장 (알림 없음)")
    parser.add_argument("--test", action="store_true", help="Discord 웹훅 연결 테스트")
    parser.add_argument("--days", type=int, default=730, help="최근 N일 이내 출판 도서만 (기본 730)")
    args = parser.parse_args()

    config = load_config()
    webhook_url = config.get("discord_webhook", "")
    stores = config.get("stores", ["aladin", "yes24", "kyobo"])

    if args.test:
        print("[테스트] Discord 웹훅 연결 확인 중...")
        if not webhook_url:
            print("  오류: config.json에 discord_webhook가 없습니다.")
            return
        r = requests.post(
            webhook_url,
            json={"content": "✅ 이지스퍼블리싱 구매평 알림봇 연결 테스트 성공!"},
            timeout=10,
        )
        print(f"  응답: HTTP {r.status_code}")
        return

    isbn_list = load_isbns(max_days=args.days)
    state = load_state()
    cache = load_cache()

    mode = "[초기화]" if args.init else "[모니터링]"
    print(f"{mode} {datetime.now():%Y-%m-%d %H:%M} — ISBN {len(isbn_list)}개, 서점 {stores}\n")

    # 예스24 product ID 일괄 조회 (미캐싱 ISBN만)
    if "yes24" in stores:
        isbns_only = [isbn for isbn, _ in isbn_list]
        missing = [i for i in isbns_only if not cache.get(f"_yes24_pid_{i}")]
        if missing:
            print(f"[예스24] product ID 조회 중 ({len(missing)}개)...")
            yes24_batch_find_ids(missing, cache)
            save_cache(cache)

    all_new: list = []

    for isbn, pub_date in isbn_list:
        if isbn not in state:
            state[isbn] = {}

        found = []

        if "aladin" in stores:
            seen = set(state[isbn].get("aladin", []))
            reviews = get_aladin_reviews(isbn, cache)
            new = [rv for rv in reviews if rv["id"] not in seen]
            if new:
                for rv in new:
                    rv["store"] = "aladin"
                found.extend(new)
            all_ids = list(seen | {rv["id"] for rv in reviews})
            state[isbn]["aladin"] = all_ids[-300:]

        if "yes24" in stores:
            seen = set(state[isbn].get("yes24", []))
            reviews = get_yes24_reviews(isbn, cache)
            new = [rv for rv in reviews if rv["id"] not in seen]
            if new:
                for rv in new:
                    rv["store"] = "yes24"
                found.extend(new)
            all_ids = list(seen | {rv["id"] for rv in reviews})
            state[isbn]["yes24"] = all_ids[-300:]

        if "kyobo" in stores:
            prev_count = state[isbn].get("kyobo_count", -1)
            product_id, curr_count, title = get_kyobo_review_count(isbn, cache)
            if curr_count > 0:
                state[isbn]["kyobo_count"] = curr_count
                if prev_count >= 0 and curr_count > prev_count:
                    diff = curr_count - prev_count
                    found.append({
                        "store": "kyobo",
                        "id": f"kyobo_{isbn}_{curr_count}",
                        "reviewer": "",
                        "rating": "",
                        "text": f"구매평 {diff}개 새로 등록 (총 {curr_count}개)",
                        "title": title,
                        "link": f"https://product.kyobobook.co.kr/detail/{product_id}",
                    })

        if found:
            book_title = next((rv["title"] for rv in found if rv.get("title")), isbn)
            print(f"  {isbn} ({book_title[:30]}) → 새 구매평 {len(found)}개")
            if not args.init:
                all_new.extend(found)

        time.sleep(0.4)

    save_state(state)
    save_cache(cache)

    print(f"\n[완료] 새 구매평 {len(all_new)}개")

    if args.init:
        print("[초기화] 기준점이 저장됐습니다. 이제 cron으로 정기 실행하세요.")
        print(f"  bash {BASE_DIR}/setup_cron.sh")
    elif all_new:
        send_discord(webhook_url, all_new)
    else:
        print("[알림 없음] 새 구매평이 없습니다.")


if __name__ == "__main__":
    main()

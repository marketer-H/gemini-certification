#!/usr/bin/env python3
"""monitor.py 핵심 기능 단위 테스트"""

import json
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent

def load_config():
    with open(BASE_DIR / "config.json") as f:
        return json.load(f)

# ─── 테스트 데이터 ─────────────────────────────────────────────
SAMPLE_BELOW = [
    (8.2, "aladin", "9791163030034", "테스트 도서 A",
     "https://www.aladin.co.kr/shop/wproduct.aspx?ISBN=9791163030034"),
    (8.7, "yes24",  "9791163030195", "테스트 도서 B",
     "https://www.yes24.com/Product/Search?query=9791163030195&domain=BOOK"),
    (8.9, "kyobo",  "9791163030300", "테스트 도서 C",
     "https://product.kyobobook.co.kr/detail/S000001234567"),
]


def test_api_key():
    """1. API 키 로딩 확인"""
    config = load_config()
    api_key = config.get("claude_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    assert api_key, "API 키 없음"
    assert api_key.startswith("sk-ant-"), f"API 키 형식 오류: {api_key[:15]}..."
    print(f"  [OK] API 키 확인: {api_key[:20]}...")


def test_anthropic_import():
    """2. anthropic 패키지 import"""
    import anthropic
    print(f"  [OK] anthropic {anthropic.__version__} 설치 확인")


def test_api_call():
    """3. Claude API 실제 호출"""
    import anthropic
    config = load_config()
    api_key = config.get("claude_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=30,
        messages=[{"role": "user", "content": "안녕이라고만 답해"}],
    )
    reply = msg.content[0].text
    assert reply, "응답 없음"
    print(f"  [OK] API 호출 성공 → {reply}")


def test_generate_recommendations():
    """4. AI 권고 생성 (샘플 도서 3권)"""
    sys.path.insert(0, str(BASE_DIR))
    from monitor import generate_ai_recommendations
    config = load_config()
    api_key = config.get("claude_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    recs = generate_ai_recommendations(SAMPLE_BELOW, api_key)
    assert isinstance(recs, dict), f"반환값이 dict가 아님: {type(recs)}"
    assert len(recs) > 0, f"권고 생성 실패 (빈 dict): {recs}"
    print(f"  [OK] 권고 {len(recs)}건 생성")
    for key, val in recs.items():
        print(f"       {key}: {val}")


def test_build_html():
    """5. HTML 이메일 생성"""
    sys.path.insert(0, str(BASE_DIR))
    from monitor import build_html_email
    html = build_html_email(SAMPLE_BELOW, {}, 9.0, "2026-04-12 09:00")
    assert "<table>" in html, "table 태그 없음"
    assert "테스트 도서 A" in html, "도서명 없음"
    print(f"  [OK] HTML 생성 ({len(html)} bytes)")


def test_create_excel():
    """6. Excel 파일 생성"""
    sys.path.insert(0, str(BASE_DIR))
    from monitor import create_excel
    path = create_excel(SAMPLE_BELOW, {}, 9.0, "2026-04-12 09:00")
    assert path and Path(path).exists(), f"Excel 파일 없음: {path}"
    size = Path(path).stat().st_size
    assert size > 0, "Excel 파일 비어있음"
    print(f"  [OK] Excel 생성: {path} ({size} bytes)")
    Path(path).unlink()


def test_send_email():
    """7. 이메일 발송 (실제 발송)"""
    sys.path.insert(0, str(BASE_DIR))
    from monitor import send_email, build_html_email, create_excel
    config = load_config()
    notif = config.get("notification", {})

    recs = {"9791163030034_aladin": "테스트 권고문입니다."}
    html = build_html_email(SAMPLE_BELOW, recs, 9.0, "2026-04-12 09:00")
    excel = create_excel(SAMPLE_BELOW, recs, 9.0, "2026-04-12 09:00")

    send_email(
        notif,
        subject="[테스트] 도서 평점 리포트",
        text_body="테스트 이메일입니다.",
        html_body=html,
        attachment_path=excel,
    )
    if excel and Path(excel).exists():
        Path(excel).unlink()
    print("  [OK] 이메일 발송 완료 — 받은편지함 확인")


# ─── 실행 ──────────────────────────────────────────────────────
TESTS = [
    ("API 키 로딩",        test_api_key),
    ("anthropic import",   test_anthropic_import),
    ("Claude API 호출",    test_api_call),
    ("AI 권고 생성",       test_generate_recommendations),
    ("HTML 이메일 생성",   test_build_html),
    ("Excel 파일 생성",    test_create_excel),
    ("이메일 발송",        test_send_email),
]

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    passed = failed = 0

    for name, fn in TESTS:
        print(f"\n[테스트 {TESTS.index((name,fn))+1}] {name}")
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  [FAIL] {e}")
            failed += 1
            if target != "all":
                break

    print(f"\n{'='*40}")
    print(f"결과: {passed}통과 / {failed}실패")
    print(f"{'='*40}")

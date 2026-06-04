#!/bin/bash
# 구매평 알림봇 cron 설정 스크립트
# 실행: bash setup_cron.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$SCRIPT_DIR/venv/bin/python"
BOT="$SCRIPT_DIR/review_bot.py"
LOG="$SCRIPT_DIR/review_bot.log"

# venv python 없으면 system python3 사용
if [ ! -f "$PYTHON" ]; then
  PYTHON=$(which python3)
fi

CRON_JOB="0 14 * * * cd $SCRIPT_DIR && $PYTHON $BOT >> $LOG 2>&1"

echo "등록할 cron 작업:"
echo "  $CRON_JOB"
echo ""
echo "이 작업은 매일 오후 2시에 실행됩니다."
echo ""

# 이미 등록된 review_bot 항목 제거 후 새로 등록
(crontab -l 2>/dev/null | grep -v "review_bot.py"; echo "$CRON_JOB") | crontab -

echo "✅ cron 등록 완료. 현재 crontab:"
crontab -l | grep review_bot

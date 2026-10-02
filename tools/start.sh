#!/bin/bash
# Start the bot in the background (keeps the Mac awake while plugged in) and show its first lines.
cd "$(dirname "$0")/.." || exit 1
if pgrep -f "Python -u run_mm" >/dev/null; then echo "bot already running"; exit 0; fi
rm -f data/STOP
nohup caffeinate -is .venv/bin/python -u run_mm.py >> data/mm_live.log 2>&1 < /dev/null &
sleep 15
pgrep -f "Python -u run_mm" >/dev/null && echo "bot running" || { echo "!! bot did not start:"; tail -20 data/mm_live.log; exit 1; }
tail -8 data/mm_live.log

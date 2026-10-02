#!/bin/bash
# Stop the bot cleanly: it cancels its orders and closes every position (account ends in USDC),
# then this script waits until that has actually finished before returning.
#   tools/stop.sh              stop the bot
#   tools/stop.sh --shutdown   stop the bot, then shut the Mac down
# Use this instead of shutting down from the Apple menu: at system shutdown the network can
# go away before the bot has finished closing positions.
cd "$(dirname "$0")/.." || exit 1
if pgrep -f "Python -u run_mm" >/dev/null; then
  touch data/STOP
  echo "stopping bot (cancel orders, close positions)..."
  for _ in $(seq 1 120); do
    pgrep -f "Python -u run_mm" >/dev/null || break
    sleep 1
  done
  rm -f data/STOP
  if pgrep -f "Python -u run_mm" >/dev/null; then
    echo "!! bot still running after 120s — check: tail -50 data/mm_live.log"
    exit 1
  fi
  grep -E "all positions closed|positions may still be open|could not fully flatten|disarmed|leaving dead man" data/mm_live.log | tail -3
  if grep -E "positions may still be open|could not fully flatten" <(tail -40 data/mm_live.log) >/dev/null; then
    echo "!! some positions may still be open — check the Lighter app before shutting down"
    exit 1
  fi
  echo "bot stopped, account is in USDC"
else
  echo "bot was not running"
fi
if [ "$1" = "--shutdown" ]; then
  echo "shutting down the Mac..."
  osascript -e 'tell app "System Events" to shut down'
fi

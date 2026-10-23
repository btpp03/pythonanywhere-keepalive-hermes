#!/bin/bash
# PA side launcher: local CONNECT bridge + nezha agent.
# Idempotent: kills whatever is running, starts fresh, prints "BR=<n> AG=<n>".
cd "$HOME/nz" || { echo "no ~/nz"; exit 9; }
[ -f bridge.py ] || { echo "missing bridge.py"; exit 8; }
[ -x nezha-agent ] || { echo "missing nezha-agent"; exit 7; }

export HTTPS_PROXY=http://127.0.0.1:18081
export HTTP_PROXY=http://127.0.0.1:18081
export NO_PROXY=
export no_proxy=

pkill -f "python3 bridge.py" 2>/dev/null
pkill -x nezha-agent 2>/dev/null
sleep 1
setsid nohup python3 bridge.py >> bridge.log 2>&1 < /dev/null &
sleep 3
NZKEY="${NZKEY:?NZKEY not set}"
setsid nohup ./nezha-agent -s nezhak2.btpp.ggff.net:443 -p "$NZKEY" --tls \
  --disable-auto-update --disable-force-update --disable-command-execute -d \
  >> agent.log 2>&1 < /dev/null &
sleep 5
echo "BR=$(pgrep -f 'python3 bridge.py' | wc -l) AG=$(pgrep -x nezha-agent | wc -l)"

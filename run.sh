#!/bin/sh
# Start the iPhone control server on port 8000, reachable from other devices
# (e.g. over Tailscale at http://<mac tailscale ip>:8000). No login, so only
# use it on networks you trust.
#   ./run.sh           listen on all interfaces
#   ./run.sh --local   only this Mac (http://127.0.0.1:8000)
cd "$(dirname "$0")"
[ -x capture/capture ] || swiftc -O capture/capture.swift -o capture/capture
[ -x capture/ocr ] || swiftc -O capture/ocr.swift -o capture/ocr
HOST=0.0.0.0
if [ "$1" = "--local" ]; then
  HOST=127.0.0.1
else
  TS_IP=$(tailscale ip -4 2>/dev/null)
  [ -n "$TS_IP" ] && echo "Tailscale: http://$TS_IP:8000"
  echo "Local:     http://127.0.0.1:8000"
fi
cd server && exec ../.venv/bin/uvicorn app:app --host "$HOST" --port 8000

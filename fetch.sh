#!/usr/bin/env bash
# Reflection's daily job. When logged friction still lacks an outcome, or a
# playbook waits for review (submitted to the pending folder, or in use but
# never reviewed), open a background Reflection chat (reached from the app, not
# the chat list) and hand it the backlog; otherwise do nothing.
# The chat is an ordinary Möbius agent turn: the platform supervises it, picks
# the model from the owner's background agents, and records its cost. Every run
# with pending work gets its own chat. The report handoff rechecks the
# queue under a short lock, so overlapping runs cannot settle one entry twice.
set -euo pipefail

APP_ID="${1:?numeric app id required}"
HERE="$(dirname "$0")"
DATA="${DATA_DIR:-/data}"
STORAGE="$DATA/apps/$APP_ID"
TODAY="$(date +%F)"
RUN="$(date -u +%Y%m%dT%H%M%S)"

PENDING="$(python3 "$HERE/friction_queue.py" pending "$STORAGE" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')"
SCAN_FAILED=0
if SCAN="$(python3 "$HERE/playbook_queue.py" pending "$DATA" "$STORAGE")"; then
  STAGED="$(printf '%s' "$SCAN" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["staged"]))')"
  LIVE="$(printf '%s' "$SCAN" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["live"]))')"
else
  # A failed scan never holds back friction and never silently turns playbook
  # review off: the run is told so, and the error stays in the job's log.
  STAGED=0 LIVE=0 SCAN_FAILED=1
fi
[ "$PENDING" -gt 0 ] || [ "$STAGED" -gt 0 ] || [ "$LIVE" -gt 0 ] || [ "$SCAN_FAILED" -gt 0 ] || exit 0

python3 - "$TODAY" "$RUN" "$PENDING" "$STAGED" "$LIVE" "$SCAN_FAILED" "$STORAGE/settings.json" <<'PY' | curl -sf -X POST \
  -H "Authorization: Bearer $APP_TOKEN" -H "Content-Type: application/json" \
  --data-binary @- "${API_BASE_URL:-http://localhost:8000}/api/app-chats/start" >/dev/null
import json, sys
today, run, pending, staged, live, scan_failed, settings_path = sys.argv[1:]
try:
  settings = json.load(open(settings_path, encoding="utf-8"))
except (OSError, ValueError):
  settings = {}
# The owner's chosen agent; without one the platform uses their background
# agents list.
agent = {key: settings[key] for key in ("provider", "model") if settings.get(key)}
waiting = []
if int(pending):
  waiting.append(f"Friction entries waiting for an outcome: {pending}.")
if int(staged):
  waiting.append(f"Staged playbooks waiting for review: {staged}.")
if int(live):
  waiting.append(f"Live playbooks not yet reviewed: {live}.")
if int(scan_failed):
  waiting.append("The playbook scan failed, so playbooks could not be listed.")
print(json.dumps({
  **agent,
  "scope": f"run-{run}",
  "title": f"Reflection — {today}",
  "content": " ".join(waiting) + " Work through them following the `reflection` skill.",
}))
PY

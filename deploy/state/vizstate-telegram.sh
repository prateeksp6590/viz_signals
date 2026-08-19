#!/usr/bin/env bash
# Post market_state.py to Telegram. Invoked by vizstate.timer, or run by hand.
#
#   /bin/bash vizstate-telegram.sh [extra args passed to market_state.py]
#
# WHY IT LOOKS LIKE vizhedge-alert.sh
# -----------------------------------
# Same three lessons, learned the same expensive way:
#   1. Parse .env with grep, never `source` it. SUBSCRIBE_INSTRUMENTS contains '|'
#      and the API key contains a space; sourcing throws "command not found" and
#      mangles values.
#   2. Telegram answers HTTP 200 with {"ok":false} on a bad token, so the STATUS CODE
#      IS NOT PROOF OF DELIVERY. Check the body. That trap silently swallowed every
#      notification for days in early August.
#   3. Invoke via /bin/bash from systemd, never the script path, so a missing exec
#      bit (scp from Windows drops it) cannot disable this.
#
# Unlike the alerter this EXITS NON-ZERO on a failed send: nothing is masked by
# doing so, and a state report that silently stops arriving is the failure mode this
# whole stack keeps repeating.

set -uo pipefail

REPO="${VIZSIGNALS_DIR:-/home/ubuntu/viz_signals}"
ENV_FILE="${VIZSIGNALS_ENV:-$REPO/.env}"
PY="${VIZSIGNALS_PY:-$REPO/.venv/bin/python}"
MAXLEN=3800          # Telegram caps a message at 4096; leave room for the wrapper

# The whitespace strip runs TWICE — before and after the quotes come off. Otherwise
# VALUE="  -100999  " unquotes to "  -100999  " with the padding intact, because the
# outer trim saw a quote at each end. Caught by test, not by inspection; the same
# flaw was in vizhedge-alert.sh and is fixed there too.
get() {
    grep -m1 "^${1}=" "$ENV_FILE" 2>/dev/null \
      | cut -d= -f2- \
      | sed -e 's/[[:space:]]*#.*$//' \
            -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
            -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/" \
            -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
      | tr -d '\r'
}

TOKEN="$(get TELEGRAM_BOT_TOKEN)"
CHAT="$(get TELEGRAM_CHAT_ID)"
if [ -z "$TOKEN" ] || [ -z "$CHAT" ]; then
    echo "vizstate: TELEGRAM_BOT_TOKEN/CHAT_ID missing from $ENV_FILE" >&2
    exit 1
fi

BODY="$(cd "$REPO" && "$PY" utils/market_state.py "$@" 2>&1)"
RC=$?
if [ -z "$BODY" ]; then
    BODY="market_state.py produced no output (exit $RC)"
fi

# <pre> keeps the columns aligned in the Telegram client; escape HTML first or a
# stray '<' silently truncates the message body.
ESCAPED="$(printf '%s' "$BODY" \
    | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' \
    | head -c "$MAXLEN")"
if [ "${#BODY}" -gt "$MAXLEN" ]; then
    ESCAPED="${ESCAPED}
... truncated, run it on the box for the full report"
fi

RESP="$(curl -sS --max-time 25 \
    -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${CHAT}" \
    --data-urlencode "parse_mode=HTML" \
    --data-urlencode "text=<pre>${ESCAPED}</pre>" 2>&1)"

case "$RESP" in
    *'"ok":true'*) echo "vizstate: posted (${#BODY} chars, market_state exit $RC)"; exit 0 ;;
    *)             echo "vizstate: SEND FAILED -> $RESP" >&2; exit 1 ;;
esac

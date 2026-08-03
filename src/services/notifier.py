"""
Push strong signals to a phone. Transport-pluggable, and never in the hot path.

Why a background thread
-----------------------
We spent considerable effort proving that entry latency destroys this strategy:
delaying entry by 2s costs 61% of P&L, 5s makes it negative. An HTTP call to Meta
or Telegram takes 200-800ms. Doing that inline would put a notification API on the
critical path of a 2-second edge. So sends are queued and drained by a daemon
thread; the poll loop never blocks and a dead notification provider can never stall
or crash trading.

Backends
--------
  log       default. Writes the alert to the log only. Zero setup, zero risk.
  ntfy      ntfy.sh — no account, no approval. Curl-simple push to a phone app.
  telegram  Bot API. Free, unlimited, no templates, ~5 minutes to set up.
  whatsapp  Meta WhatsApp Cloud API. Requires a Meta Business account, a verified
            number and a PRE-APPROVED TEMPLATE: business-initiated messages cannot
            be free-form. Use the Utility category (cheapest, and free when
            delivered inside an open 24h customer-service window).
  twilio    Twilio's WhatsApp wrapper. Easier onboarding, sandbox for testing;
            still needs an approved template for business-initiated sends.
"""

import json
import queue
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from ..config import settings
from ..utils.logger import logger

IST = timezone(timedelta(hours=5, minutes=30))
_SENTINEL = object()


def _post(url: str, data: bytes, headers: dict, timeout: float = 10.0) -> tuple[int, str]:
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read(400).decode('utf-8', 'replace')


class Notifier:
    def __init__(self):
        self.backend = (settings.NOTIFY_BACKEND or 'log').lower()
        self._q: queue.Queue = queue.Queue(maxsize=200)
        self._last: dict[str, datetime] = {}
        self._thread = None
        self._sent = self._failed = self._suppressed = 0
        self._day = datetime.now(IST).date()
        self._day_count = 0
        if self.backend != 'log':
            self._thread = threading.Thread(target=self._drain, daemon=True,
                                            name='notifier')
            self._thread.start()
        logger.info(f'Notifier backend={self.backend} '
                    f'min_ratio={settings.NOTIFY_MIN_ANGLE_RATIO} '
                    f'min_move={settings.NOTIFY_MIN_MOVE_PCT}% '
                    f'cooldown={settings.NOTIFY_COOLDOWN_SECS}s')

    # ── what counts as "strong" ──────────────────────────────────────────────
    def should_notify(self, sig) -> tuple[bool, str]:
        if self.backend == 'off':
            return False, 'disabled'
        if settings.NOTIFY_ACTIONS and sig.action.value not in settings.NOTIFY_ACTIONS:
            return False, f'action {sig.action.value} not in NOTIFY_ACTIONS'

        m = sig.meta or {}
        angle, thr = m.get('angle_deg'), m.get('threshold_deg')
        # How far past its own bar did it clear? Measured on the 30 Jul SENSEX move,
        # the genuine breakout ran ratio 1.43 while marginal triggers sat at 1.07 —
        # so this ratio separates "worth a phone buzz" from "worth a journal line".
        if angle and thr:
            ratio = angle / thr
            if ratio < settings.NOTIFY_MIN_ANGLE_RATIO:
                return False, f'ratio {ratio:.2f} < {settings.NOTIFY_MIN_ANGLE_RATIO}'
        move = self._recent_move_pct(m)
        if move is not None and abs(move) < settings.NOTIFY_MIN_MOVE_PCT:
            return False, f'move {move:.2f}% < {settings.NOTIFY_MIN_MOVE_PCT}%'

        now = datetime.now(IST)
        if now.date() != self._day:                 # new session, reset the budget
            self._day, self._day_count = now.date(), 0
        if settings.NOTIFY_MAX_PER_DAY and self._day_count >= settings.NOTIFY_MAX_PER_DAY:
            return False, f'daily cap {settings.NOTIFY_MAX_PER_DAY} reached'
        last = self._last.get(sig.instrument_key)
        if last and (now - last).total_seconds() < settings.NOTIFY_COOLDOWN_SECS:
            return False, 'cooldown'
        self._last[sig.instrument_key] = now
        self._day_count += 1
        return True, ''

    @staticmethod
    def _recent_move_pct(meta: dict):
        """% the price moved over the recent leg (slope is per-tick in pct mode)."""
        s, n1 = meta.get('slope_recent'), meta.get('n1')
        return None if s is None or not n1 else s * n1

    # ── public ───────────────────────────────────────────────────────────────
    def notify(self, sig) -> None:
        ok, why = self.should_notify(sig)
        if not ok:
            self._suppressed += 1
            logger.debug(f'notify suppressed [{why}]: {sig.symbol}')
            return
        text = self._format(sig)
        if self.backend == 'log':
            logger.info(f'ALERT {text}')
            self._sent += 1
            return
        try:
            self._q.put_nowait((sig, text))
        except queue.Full:
            self._failed += 1
            logger.warning('notifier queue full — dropping alert')

    def _format(self, sig) -> str:
        m = sig.meta or {}
        move = self._recent_move_pct(m)
        return (f"{sig.action.value.replace('ENTER_', '')} {sig.symbol} @ {sig.price:.2f} | "
                f"angle {m.get('angle_deg', 0):.2f} vs {m.get('threshold_deg', 0):.2f}"
                + (f" | {move:+.2f}% recent" if move is not None else '')
                + f" | {datetime.now(IST):%H:%M:%S} IST")

    def close(self) -> None:
        if self._thread:
            self._q.put(_SENTINEL)
            self._thread.join(timeout=8)
        logger.info(f'Notifier: {self._sent} sent, {self._failed} failed, '
                    f'{self._suppressed} suppressed')

    # ── background sender ────────────────────────────────────────────────────
    def _drain(self) -> None:
        while True:
            item = self._q.get()
            if item is _SENTINEL:
                break
            sig, text = item
            for attempt in range(3):
                try:
                    self._send(sig, text)
                    self._sent += 1
                    break
                except Exception as e:
                    if attempt == 2:
                        self._failed += 1
                        logger.error(f'notify failed after 3 tries ({self.backend}): '
                                     f'{" ".join(str(e).split())[:180]}')
                    else:
                        time.sleep(2 ** attempt)

    def _send(self, sig, text: str) -> None:
        b = self.backend
        if b == 'ntfy':
            _post(f'{settings.NTFY_URL.rstrip("/")}/{settings.NTFY_TOPIC}',
                  text.encode(), {'Title': 'viz_signals', 'Priority': 'high'})

        elif b == 'telegram':
            # Telegram can answer HTTP 200 with {"ok": false, ...}. Checking only the
            # status code makes "sent" mean "accepted by the socket", not "delivered
            # to a chat" -- so a wrong chat_id looks like success forever.
            status, body = _post(
                f'https://api.telegram.org/bot{settings.TELEGRAM_TOKEN}/sendMessage',
                urllib.parse.urlencode({'chat_id': settings.TELEGRAM_CHAT_ID,
                                        'text': text}).encode(),
                {'Content-Type': 'application/x-www-form-urlencoded'})
            try:
                payload = json.loads(body)
            except Exception:
                payload = {}
            if payload.get('ok') is False:
                raise RuntimeError(
                    f"telegram rejected: {payload.get('description', body[:120])} "
                    f"(chat_id={settings.TELEGRAM_CHAT_ID})")

        elif b == 'whatsapp':
            # Business-initiated => must use an approved template. Variables are
            # positional and must match the template body exactly.
            m = sig.meta or {}
            params = [sig.action.value.replace('ENTER_', ''), sig.symbol,
                      f'{sig.price:.2f}', f"{m.get('angle_deg', 0):.2f}",
                      f"{m.get('threshold_deg', 0):.2f}",
                      f'{datetime.now(IST):%H:%M:%S}']
            payload = {
                'messaging_product': 'whatsapp',
                'to': settings.WHATSAPP_TO,
                'type': 'template',
                'template': {
                    'name': settings.WHATSAPP_TEMPLATE,
                    'language': {'code': settings.WHATSAPP_LANG},
                    'components': [{'type': 'body',
                                    'parameters': [{'type': 'text', 'text': p}
                                                   for p in params]}],
                },
            }
            _post(f'https://graph.facebook.com/v21.0/'
                  f'{settings.WHATSAPP_PHONE_ID}/messages',
                  json.dumps(payload).encode(),
                  {'Authorization': f'Bearer {settings.WHATSAPP_TOKEN}',
                   'Content-Type': 'application/json'})

        elif b == 'twilio':
            auth = urllib.parse.quote(settings.TWILIO_SID) + ':' + \
                   urllib.parse.quote(settings.TWILIO_TOKEN)
            import base64
            _post(f'https://api.twilio.com/2010-04-01/Accounts/'
                  f'{settings.TWILIO_SID}/Messages.json',
                  urllib.parse.urlencode({'From': settings.TWILIO_FROM,
                                          'To': settings.TWILIO_TO,
                                          'Body': text}).encode(),
                  {'Authorization': 'Basic ' + base64.b64encode(auth.encode()).decode(),
                   'Content-Type': 'application/x-www-form-urlencoded'})
        else:
            raise ValueError(f'unknown NOTIFY_BACKEND {b!r}')

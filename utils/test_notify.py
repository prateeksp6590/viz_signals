"""
Verify alerting without waiting for a live signal.

    python utils/test_notify.py --discover     # Telegram: find your chat_id
    python utils/test_notify.py                # send one sample alert
    python utils/test_notify.py --filters      # show what would/wouldn't alert

Run it on the box the engine runs on: it uses the same .env and the same code path.
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv()

from src.config import settings                     # noqa: E402
from src.models import Signal, SignalAction         # noqa: E402
from src.services.notifier import Notifier          # noqa: E402


def discover():
    """Telegram hands out chat_id only after YOU message the bot first."""
    tok = settings.TELEGRAM_TOKEN
    if not tok:
        sys.exit('TELEGRAM_TOKEN is not set in .env')
    url = f'https://api.telegram.org/bot{tok}/getUpdates'
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as e:
        sys.exit(f'getUpdates failed: {e}\n'
                 'Check the token, and that the box can reach api.telegram.org.')
    if not data.get('ok'):
        sys.exit(f'Telegram says: {data}')
    results = data.get('result', [])
    if not results:
        print('No messages yet.\n'
              '  1. open Telegram, find your bot, press START\n'
              '  2. send it any message ("hi")\n'
              '  3. run this again')
        return
    seen = {}
    for u in results:
        msg = u.get('message') or u.get('channel_post') or {}
        chat = msg.get('chat') or {}
        if chat.get('id'):
            seen[chat['id']] = f"{chat.get('type')} {chat.get('first_name') or chat.get('title') or ''}"
    print('chat_id(s) that have messaged this bot:')
    for cid, who in seen.items():
        print(f'  TELEGRAM_CHAT_ID={cid}    ({who.strip()})')


def sample_signal(angle=12.51, thr=8.76):
    return Signal(instrument_key='BSE_FO|1145633',
                  symbol='SENSEX 77500 CE 06 AUG 26',
                  action=SignalAction.ENTER_LONG, price=206.80,
                  strategy='slope_angle',
                  reason='test alert from utils/test_notify.py',
                  meta={'angle_deg': angle, 'threshold_deg': thr,
                        'slope_recent': 0.17, 'n1': 50, 'n2': 80})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--discover', action='store_true')
    ap.add_argument('--filters', action='store_true')
    args = ap.parse_args()

    print(f'backend            : {settings.NOTIFY_BACKEND}')
    print(f'min angle ratio    : {settings.NOTIFY_MIN_ANGLE_RATIO}')
    print(f'cooldown / cap     : {settings.NOTIFY_COOLDOWN_SECS}s / '
          f'{settings.NOTIFY_MAX_PER_DAY} per day')
    print(f'actions            : {settings.NOTIFY_ACTIONS}')
    if settings.NOTIFY_BACKEND == 'telegram':
        tok = settings.TELEGRAM_TOKEN
        print(f'telegram token     : {"set (" + tok[:8] + "…)" if tok else "MISSING"}')
        print(f'telegram chat_id   : {settings.TELEGRAM_CHAT_ID or "MISSING"}')
    print()

    if args.discover:
        return discover()

    n = Notifier()
    if args.filters:
        print(f"  {'angle':>7}{'thresh':>8}{'ratio':>7}  decision")
        print('  ' + '-' * 46)
        for a, t in ((12.51, 8.76), (9.13, 8.50), (2.12, 2.07), (5.68, 8.42), (20.0, 8.0)):
            n._last.clear(); n._day_count = 0
            ok, why = n.should_notify(sample_signal(a, t))
            print(f'  {a:>7.2f}{t:>8.2f}{a/t:>7.2f}  '
                  f'{"ALERT" if ok else "quiet — " + why}')
        return

    if settings.NOTIFY_BACKEND == 'log':
        print('NOTIFY_BACKEND=log — nothing will leave the box. '
              'Set NOTIFY_BACKEND=telegram to actually send.\n')
    n.notify(sample_signal())
    n.close()
    print('\nIf the backend is telegram and no message arrived, the error is in the '
          'log above (the sender retries 3x before giving up).')


if __name__ == '__main__':
    main()

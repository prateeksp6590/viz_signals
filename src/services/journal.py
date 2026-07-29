"""Audit trail: append-only JSONL per day, plus optional InfluxDB persistence
into a signals_YYYYMMDD bucket so signals/orders/P&L can be charted next to
the tick data (docs §10).

Position snapshots with event='mark' are high-volume (one per open position
per cycle) — they go to InfluxDB only; JSONL keeps open/close events.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import requests
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from ..config import settings
from ..models import Fill, Order, Position, Signal
from ..utils.logger import logger


class Journal:
    def __init__(self):
        self._date_str = datetime.now().strftime('%Y%m%d')
        self._dir = settings.JOURNAL_DIR / self._date_str
        self._dir.mkdir(parents=True, exist_ok=True)

        self._influx_write = None
        if settings.PERSIST_TO_INFLUX:
            try:
                self._client = InfluxDBClient(
                    url=settings.INFLUX_URL,
                    token=settings.INFLUX_TOKEN,
                    org=settings.INFLUX_ORG,
                )
                self._ensure_bucket(self.bucket)
                self._influx_write = self._client.write_api(write_options=SYNCHRONOUS)
            except Exception as e:
                logger.error(f'Influx journal disabled — {e}')

    @property
    def bucket(self) -> str:
        # ONE bucket for all days (dateless). Per-day buckets exhaust InfluxDB
        # Cloud's bucket-count quota; the point timestamp separates days.
        return settings.SIGNALS_BUCKET

    # ── event API ─────────────────────────────────────────────────────────────

    def signal(self, sig: Signal) -> None:
        self._jsonl('signals', sig.to_dict())
        pt = (Point('signal')
              .tag('symbol', sig.symbol)
              .tag('strategy', sig.strategy)
              .tag('action', sig.action.value)
              .tag('exch', sig.instrument_key.split('|', 1)[0].split('_', 1)[0])
              .field('price', float(sig.price))
              .field('confidence', float(sig.confidence))
              .time(sig.ts, WritePrecision.MS))
        # strategy diagnostics (angle_deg, slopes, thresholds ...) so triggers can
        # be charted and re-tuned in Grafana next to the tick data
        for k, v in (sig.meta or {}).items():
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            pt.field(k, float(v))
        self._point(pt)

    def order(self, order: Order) -> None:
        self._jsonl('orders', order.to_dict())
        self._point(
            Point('order')
            .tag('symbol', order.symbol)
            .tag('side', order.side.value)
            .tag('status', order.status.value)
            .tag('mode', order.mode)
            .field('qty', int(order.qty))
            .time(order.ts, WritePrecision.MS)
        )

    def fill(self, f: Fill) -> None:
        self._jsonl('fills', f.to_dict())

    def rejection(self, sig: Signal, reason: str) -> None:
        self._jsonl('rejections', {**sig.to_dict(), 'reject_reason': reason})

    def position(self, pos: Position, event: str) -> None:
        if event in ('open', 'close'):
            self._jsonl('positions', {**pos.to_dict(), 'event': event})
        p = (
            Point('position')
            .tag('symbol', pos.symbol)
            .tag('side', pos.side)
            .tag('strategy', pos.strategy)
            .tag('event', event)
            .field('last_price', float(pos.last_price))
            .field('unrealized_pnl', float(pos.unrealized_pnl))
            .field('max_favorable', float(pos.max_favorable))
            .field('max_adverse', float(pos.max_adverse))
            .field('qty', int(pos.qty))
            .time(datetime.now(timezone.utc), WritePrecision.MS)
        )
        if pos.realized_pnl is not None:
            p.field('realized_pnl', float(pos.realized_pnl))
        self._point(p)

    # ── sinks ─────────────────────────────────────────────────────────────────

    def _jsonl(self, name: str, record: dict) -> None:
        try:
            with open(self._dir / f'{name}.jsonl', 'a', encoding='utf-8') as f:
                f.write(json.dumps(record, default=str) + '\n')
        except Exception as e:
            logger.error(f'Journal write failed ({name}): {e}')

    def _point(self, point: Point) -> None:
        if not self._influx_write:
            return
        try:
            self._influx_write.write(
                bucket=self.bucket, org=settings.INFLUX_ORG, record=point
            )
        except Exception as e:
            logger.error(f'Influx journal write failed: {e}')

    def _ensure_bucket(self, bucket: str) -> None:
        res = requests.get(
            f'{settings.INFLUX_URL}/api/v2/orgs',
            params={'org': settings.INFLUX_ORG},
            headers={'Authorization': f'Token {settings.INFLUX_TOKEN}'},
        )
        res.raise_for_status()
        orgs = res.json().get('orgs', [])
        if not orgs:
            raise RuntimeError(f'InfluxDB org "{settings.INFLUX_ORG}" not found')
        res = requests.post(
            f'{settings.INFLUX_URL}/api/v2/buckets',
            headers={
                'Authorization': f'Token {settings.INFLUX_TOKEN}',
                'Content-Type': 'application/json',
            },
            json={'name': bucket, 'orgID': orgs[0]['id'], 'retentionRules': []},
        )
        if res.status_code == 422:
            return  # already exists
        if not res.ok:
            raise RuntimeError(f'Failed to create bucket "{bucket}": {res.text}')
        logger.info(f'Created InfluxDB bucket: {bucket}')

    def close(self) -> None:
        if self._influx_write:
            try:
                self._influx_write.close()
                self._client.close()
            except Exception:
                pass

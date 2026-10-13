"""Offline flow-neutral unit-NAV ledger; NOT an exchange fill accountant.

INIT: initialize verified strategy equity, NAV=1.
MARK: supply total verified strategy equity INCLUDING unrealized P&L and all
      exchange fees/funding already reflected in equity. Do not subtract twice.
FLOW: deposit (+) or withdrawal (-), with verified equity immediately BEFORE
      the flow. The ledger values units at the pre-flow NAV.

External server/API costs are not automatically included: maintain a separate
project-expense ledger. A transfer from a project reserve is a FLOW to this
strategy ledger, but is NOT new external capital to the whole project.
"""
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional
from risk_core import number, signed_number


class UnitLedger:
    def __init__(self, path: str = ':memory:') -> None:
        if path != ':memory:':
            p = Path(path)
            if not p.parent.exists():
                raise ValueError('ledger parent directory does not exist')
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA synchronous=FULL')
        self.connection.execute('''CREATE TABLE IF NOT EXISTS nav_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            timestamp_utc TEXT NOT NULL,
            payload TEXT NOT NULL,
            equity TEXT NOT NULL,
            units TEXT NOT NULL,
            net_contributions TEXT NOT NULL,
            nav TEXT NOT NULL,
            peak_nav TEXT NOT NULL
        )''')
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _timestamp(value: str) -> str:
        try:
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except (AttributeError, ValueError) as exc:
            raise ValueError('timestamp must be ISO-8601 with timezone') from exc
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError('timestamp must include timezone')
        return stamp.astimezone(timezone.utc).isoformat(timespec='microseconds')

    def append(self, *, event_id: str, timestamp: str, kind: str, amount: Any,
               pre_equity: Optional[Any] = None) -> bool:
        """Atomically append. Exact repeated IDs are no-ops; conflicts raise.

        FLOW requires an immediate pre-flow valuation. To represent several
        intraday flows, append each separately in confirmed chronological order.
        This demo rejects a complete withdrawal and cannot restart a wiped-out
        NAV; closing/restarting mandates a new, explicitly separate series.
        """
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError('event_id is required')
        stamp = self._timestamp(timestamp)
        if kind not in {'INIT', 'MARK', 'FLOW'}:
            raise ValueError('kind must be INIT, MARK, or FLOW')
        value = signed_number(amount, 'amount') if kind == 'FLOW' else number(amount, 'amount')
        if kind == 'FLOW':
            if pre_equity is None:
                raise ValueError('FLOW requires verified pre_equity')
            pre = number(pre_equity, 'pre_equity', strictly_positive=True)
            if value == 0:
                raise ValueError('zero cash flow is not meaningful')
        else:
            if pre_equity is not None:
                raise ValueError('pre_equity applies only to FLOW')
            pre = None
        payload = json.dumps({'timestamp': stamp, 'kind': kind, 'amount': str(value),
                              'pre_equity': None if pre is None else str(pre)},
                             sort_keys=True, separators=(',', ':'))
        con = self.connection
        con.execute('BEGIN IMMEDIATE')
        try:
            duplicate = con.execute('SELECT payload FROM nav_events WHERE event_id=?',
                                    (event_id,)).fetchone()
            if duplicate is not None:
                if duplicate['payload'] != payload:
                    raise ValueError('event ID conflict: same ID, different content')
                con.commit()
                return False
            last = con.execute('SELECT * FROM nav_events ORDER BY sequence DESC LIMIT 1').fetchone()
            if last is None:
                if kind != 'INIT' or value <= 0:
                    raise ValueError('first event must be INIT with positive equity')
                equity = units = contributions = value
                nav = peak = Decimal('1')
            else:
                if kind == 'INIT':
                    raise ValueError('INIT cannot reset an existing performance series')
                if stamp < last['timestamp_utc']:
                    raise ValueError('out-of-order event; reconcile and replay explicitly')
                units = Decimal(last['units'])
                contributions = Decimal(last['net_contributions'])
                peak = Decimal(last['peak_nav'])
                if kind == 'MARK':
                    equity = value
                    nav = equity / units
                    peak = max(peak, nav)
                else:
                    if Decimal(last['nav']) <= 0:
                        raise ValueError('cannot recapitalize a zero-NAV series silently')
                    before_nav = pre / units
                    peak = max(peak, before_nav)
                    equity = pre + value
                    if equity <= 0:
                        raise ValueError('complete withdrawal/negative equity needs explicit closure')
                    units += value / before_nav
                    contributions += value
                    nav = before_nav  # a pure cash flow never changes NAV
            con.execute('''INSERT INTO nav_events
                           (event_id,timestamp_utc,payload,equity,units,
                            net_contributions,nav,peak_nav)
                           VALUES (?,?,?,?,?,?,?,?)''',
                        (event_id, stamp, payload, str(equity), str(units),
                         str(contributions), str(nav), str(peak)))
            con.commit()
            return True
        except Exception:
            con.rollback()
            raise

    def report(self) -> Dict[str, str]:
        row = self.connection.execute('SELECT * FROM nav_events ORDER BY sequence DESC LIMIT 1').fetchone()
        if row is None:
            return {'status': 'EMPTY'}
        equity, contributions = Decimal(row['equity']), Decimal(row['net_contributions'])
        nav, peak = Decimal(row['nav']), Decimal(row['peak_nav'])
        return {'status': 'OFFLINE_LEDGER', 'as_of_utc': row['timestamp_utc'],
                'equity_usdt': str(equity), 'units': row['units'], 'nav': str(nav),
                'flow_neutral_return_fraction': str(nav - 1),
                'drawdown_fraction': str(1 - nav / peak),
                'peak_nav': str(peak), 'net_contributions_usdt': str(contributions),
                'trading_pnl_usdt': str(equity - contributions),
                'external_operating_costs': 'NOT_INCLUDED',
                'event_count': str(self.connection.execute('SELECT COUNT(*) FROM nav_events').fetchone()[0])}

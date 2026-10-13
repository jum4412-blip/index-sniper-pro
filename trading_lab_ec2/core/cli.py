#!/usr/bin/env python3
"""No API keys or internet required. This CLI cannot place real trades."""
import argparse
import json
import sys
from risk_core import Policy, plan_size, proposed_capital_ceiling
from ledger import UnitLedger


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def demo():
    # ALL prices, cost allowances, quantity rules and balances below are fictional.
    # In particular, these are NOT downloaded Bitget instrument specifications.
    shared = dict(symbol='BTCUSDT', side='long', account_equity='700',
                  approved_capital='700', entry='80000', structural_stop='78800',
                  cost_fraction='0.002', qty_step='0.0001', min_qty='0.0001',
                  min_notional='5', available_initial_margin='700',
                  account_reconciled=True, market_data_fresh=True,
                  instrument_rules_verified=True, protection_capability_verified=True)
    ledger = UnitLedger()
    ledger.append(event_id='sample_init', timestamp='2026-10-13T00:00:00Z', kind='INIT', amount='700')
    ledger.append(event_id='sample_loss', timestamp='2026-10-14T00:00:00Z', kind='MARK', amount='630')
    before = ledger.report()
    ledger.append(event_id='sample_deposit', timestamp='2026-10-15T00:00:00Z',
                  kind='FLOW', amount='200', pre_equity='630')
    after = ledger.report()
    ledger.close()
    output({'IMPORTANT': '가상 숫자로 계산만 합니다. 실매매·시장 접속·API 연동 없음.',
            'micro_example': plan_size(policy=Policy(stage='micro'), **shared),
            'standard_example': plan_size(policy=Policy(stage='standard'), **shared),
            'before_deposit': before, 'after_deposit': after,
            'capital_review_example': proposed_capital_ceiling(
                previous_capital='700', project_total='1130', evidence_green=True,
                unresolved_incident=False, drawdown='0')})


def main():
    parser = argparse.ArgumentParser(description='1030 USDT offline research/accounting starter; NO LIVE ORDERS')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('demo', help='가상 위험계산 및 입금 중립 수익률 예제')
    item = sub.add_parser('append', help='로컬 NAV 장부에 이벤트 하나 기록')
    item.add_argument('--db', required=True)
    item.add_argument('--id', required=True, dest='event_id')
    item.add_argument('--timestamp', required=True)
    item.add_argument('--kind', choices=['INIT', 'MARK', 'FLOW'], required=True)
    item.add_argument('--amount', required=True)
    item.add_argument('--pre-equity')
    report = sub.add_parser('report', help='로컬 장부 결과 확인')
    report.add_argument('--db', required=True)
    args = parser.parse_args()
    if args.command == 'demo':
        demo()
        return
    if args.command == 'report':
        from pathlib import Path
        if not Path(args.db).is_file():
            parser.error('ledger file does not exist')
    ledger = UnitLedger(args.db)
    try:
        if args.command == 'append':
            changed = ledger.append(event_id=args.event_id, timestamp=args.timestamp,
                                    kind=args.kind, amount=args.amount,
                                    pre_equity=args.pre_equity)
            output({'appended': changed, 'report': ledger.report()})
        else:
            output(ledger.report())
    finally:
        ledger.close()


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(2)

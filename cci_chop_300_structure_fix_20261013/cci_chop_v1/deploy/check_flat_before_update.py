#!/usr/bin/env python3
"""Read-only exchange preflight, usable from a staged update directory."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from cci_chop_v1.recovery import check_flat
from cci_chop_v1._compat.core import SafetyError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    try:
        snap = check_flat(args.root.resolve())
        print("거래소 전체 조회 성공: 포지션 0 / 미체결 주문 0 / 전략 주문 0. 주문 전송 없음.")
        return 0
    except (SafetyError, ValueError, TypeError, OSError) as exc:
        print("BLOCKED:", str(exc) if isinstance(exc, SafetyError) else type(exc).__name__)
        print("실제 포지션·주문이 남았거나 조회에 실패했습니다. 설치·상태 이전을 진행하지 않습니다.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

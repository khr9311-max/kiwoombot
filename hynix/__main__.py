"""python -m hynix backfill | summary"""
from __future__ import annotations

import argparse
import logging
import sys

from . import collect, store


def main() -> int:
    ap = argparse.ArgumentParser(prog="hynix")
    ap.add_argument("command", choices=["backfill", "summary"])
    ap.add_argument("--no-kiwoom", action="store_true", help="키움 TR 수집 건너뛰기")
    ap.add_argument("--no-external", action="store_true", help="yfinance 수집 건너뛰기")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store.init()
    if args.command == "backfill":
        collect.backfill(kiwoom=not args.no_kiwoom, external=not args.no_external)
    print("\n=== 수집 현황 ===")
    print(store.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())

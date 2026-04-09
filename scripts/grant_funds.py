from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Grant internal funds to a lobster account.")
    parser.add_argument("claw_id")
    parser.add_argument("amount", type=int)
    parser.add_argument("--note", default="")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    account = store.grant_funds_by_claw_id(args.claw_id.strip().upper(), args.amount, note=args.note)
    print(json.dumps(dict(account), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

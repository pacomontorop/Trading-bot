#!/usr/bin/env python3
"""Summarize trading events for a given date."""

import argparse
from datetime import datetime, date
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FILE = os.path.join(PROJECT_ROOT, "logs", "events.log")


def parse_args():
    parser = argparse.ArgumentParser(description="Summarize trading log events")
    parser.add_argument(
        "-d",
        "--date",
        default=date.today().isoformat(),
        help="Date to summarize in YYYY-MM-DD format. Defaults to today.",
    )
    return parser.parse_args()


def summarize(log_path: str, target_date: str) -> None:
    success = 0
    failures = 0
    shorts = 0
    errors = 0

    if not os.path.exists(log_path):
        print(f"No log file found at {log_path}")
        return

    with open(log_path, encoding="utf-8") as f:
        for line in f:
            if not line.startswith("["):
                continue
            try:
                timestamp_str, msg = line.strip().split("]", 1)
                timestamp_str = timestamp_str.lstrip("[")
                ts_date = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S").date()
            except Exception:
                continue

            if ts_date.isoformat() != target_date:
                continue

            if "Short y trailing buy" in msg:
                shorts += 1
            if "✅" in msg and (
                "Orden enviada" in msg
                or "Compra y trailing stop" in msg
                or "Short y trailing buy" in msg
            ):
                success += 1
            if "Falló la orden" in msg:
                failures += 1
            if any(k in msg for k in ["❌", "⚠️", "⛔", "Error"]):
                errors += 1

    print(f"📅 Summary for {target_date}")
    print(f"✅ Successful orders: {success}")
    print(f"❌ Failed orders: {failures}")
    print(f"📉 Shorts executed: {shorts}")
    print(f"⚠️ Errors: {errors}")


def main() -> None:
    args = parse_args()
    summarize(LOG_FILE, args.date)


if __name__ == "__main__":
    main()

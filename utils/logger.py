import os
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
log_dir = os.path.join(PROJECT_ROOT, "logs")


def log_event(message, **fields):
    os.makedirs(log_dir, exist_ok=True)
    trading_file = os.path.join(log_dir, "trading.log")
    approval_file = os.path.join(log_dir, "approvals.log")

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    log_line = f"[{timestamp}] {message}" + (f" {extra}" if extra else "")

    print(log_line)
    if message.startswith("APPROVAL"):
        with open(approval_file, "a", encoding="utf-8") as f:
            f.write(log_line + "\n")
    else:
        with open(trading_file, "a", encoding="utf-8") as f:
            f.write(log_line + "\n")

import os
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
log_dir = os.path.join(PROJECT_ROOT, "logs")


def log_event(message, **fields):
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "events.log")

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    log_line = f"[{timestamp}] {message}" + (f" {extra}" if extra else "")

    print(log_line)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(log_line + "\n")

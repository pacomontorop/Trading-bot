from datetime import datetime

def log_event(message):
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {message}"
    print(log_line)
    with open("log.txt", "a", encoding="utf-8") as log_file:
        log_file.write(log_line + "\n")
# log_event con logging

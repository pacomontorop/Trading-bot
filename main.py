from core.scheduler import start_schedulers
import time

if __name__ == "__main__":
    print("🟢 Lanzando schedulers...")
    start_schedulers()

    # 🔁 Mantener vivo el proceso aunque todos los hilos sean daemon
    while True:
        time.sleep(3600)

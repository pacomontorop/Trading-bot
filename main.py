from core.scheduler import start_schedulers
import time

if __name__ == "__main__":
    print("🟢 Lanzando schedulers...")
    start_schedulers()
    while True:
        print("⏳ Main thread activo, esperando...")
        time.sleep(600)

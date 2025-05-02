from core.scheduler import start_schedulers
import traceback

if __name__ == "__main__":
    print("🟢 Lanzando schedulers...")

    try:
        start_schedulers()
    except Exception as e:
        print("❌ Error en el proceso principal:")
        traceback.print_exc()

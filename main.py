from core.scheduler import start_schedulers
import time

def main():
    print("Iniciando schedulers...")
    start_schedulers()  # lanza los hilos, procesos o jobs programados
    while True:
        print("Esperando...")
        time.sleep(60)

if __name__ == "__main__":
    main()

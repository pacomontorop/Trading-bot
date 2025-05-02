from core.scheduler import start_schedulers

import time

def main():
    while True:
        print("Ejecutando tareas...")
        # Aquí va tu lógica de trading, lectura de señales, etc.
        time.sleep(60)  # espera 1 minuto antes de repetir

if __name__ == "__main__":
    main()

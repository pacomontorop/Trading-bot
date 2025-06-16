# Trading Bot

Estructura modular del bot de trading.

## Instalación

1. Crea un entorno virtual y actívalo.
2. Instala las dependencias necesarias:

```bash
pip install -r requirements.txt
```

## Variables de entorno

El bot utiliza variables definidas en un archivo `.env` en la raíz del repositorio.
Debes proporcionar al menos las siguientes claves:

- `APCA_API_KEY_ID` y `APCA_API_SECRET_KEY` – Credenciales de Alpaca.
- `QUIVER_API_KEY` – Clave para la API de QuiverQuant.
- `EMAIL_SENDER`, `EMAIL_RECEIVER` y `EMAIL_PASSWORD` – Datos para el envío de correos.

## Uso

Para iniciar el bot ejecuta:

```bash
python start.py
```

Esto lanzará los distintos schedulers y comenzará la monitorización de señales.

## Pruebas

Las pruebas unitarias se ejecutan con `pytest`:

```bash
pytest
```

import os

import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram_alert(message: str, verbose: bool = False) -> bool:
    """Send a simple text message via Telegram.

    Parameters
    ----------
    message: str
        Text to send.
    verbose: bool, optional
        If True, prints debug information. Default is False.

    Returns
    -------
    bool
        True if the message was sent, False otherwise.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        if verbose:
            print("Telegram not configured: missing token or chat ID")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}

    try:
        response = requests.post(url, data=payload, timeout=10)
        if verbose:
            print(f"Telegram response: {response.status_code} - {response.text}")
        response.raise_for_status()
        return True
    except Exception as exc:
        if verbose:
            print(f"Error sending Telegram message: {exc}")
        return False

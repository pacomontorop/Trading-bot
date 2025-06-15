# test_env.py
import os
from dotenv import load_dotenv

load_dotenv()
key_set = bool(os.getenv("QUIVER_API_KEY"))
print("🔑 QUIVER_API_KEY is set" if key_set else "⚠️ QUIVER_API_KEY not set")

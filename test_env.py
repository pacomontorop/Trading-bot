# test_env.py
import os
from dotenv import load_dotenv

load_dotenv()
print("🔑 QUIVER_API_KEY =", os.getenv("QUIVER_API_KEY"))

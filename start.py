# start.py
from main import app, launch_all
import uvicorn

launch_all()  # Lanza los schedulers + heartbeat

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

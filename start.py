
from main import app, launch_all
import os
import uvicorn

if __name__ == "__main__":
    launch_all()  # Lanza los schedulers + heartbeat
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
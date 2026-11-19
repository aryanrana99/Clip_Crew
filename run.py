"""Start ClipCrew:  .venv\\Scripts\\python run.py   then open http://127.0.0.1:8765"""
import os

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    print(f"\n  ClipCrew is running — open http://127.0.0.1:{port}\n  Press Ctrl+C to stop.\n")
    uvicorn.run("clipper.server:app", host="127.0.0.1", port=port, log_level="warning")

"""Start ChargeSight:  python run.py   (then open http://127.0.0.1:8000)"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))

if __name__ == "__main__":
    import uvicorn

    from app.config import get_settings

    s = get_settings()
    print(f"\n  ChargeSight running at  http://{s.host}:{s.port}\n  Press Ctrl+C to stop.\n")
    uvicorn.run("app.main:app", host=s.host, port=s.port, app_dir=str(ROOT / "backend"), log_level=s.log_level.lower())

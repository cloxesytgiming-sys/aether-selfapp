"""
Runs BOTH the keygen bot (manager.py) and the dashboard (dashboard.py) from one command:

    python start.py          (also the Railway start command)

Reads your .env itself, restarts the keygen bot if it crashes, and keeps the dashboard running
even when the keygen bot is not configured or stopped on a login error.
"""
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv
    load_dotenv(HERE / ".env")
except ImportError:
    pass


def keygen_forever():
    while True:
        code = subprocess.call([sys.executable, "manager.py"], cwd=str(HERE))
        if code == 3:  # rejected token / missing intent: restarting would only spam Discord
            print("[start] keygen bot stopped - fix the error shown above, then restart. The dashboard keeps running.", flush=True)
            return
        print(f"[start] keygen bot exited ({code}), restarting in 15s", flush=True)
        time.sleep(15)


token = os.getenv("KEYGEN_BOT_TOKEN", "").strip()
admins = [a for a in re.split(r"[,\s]+", os.getenv("KEYGEN_ADMINS", "")) if a]
if token and admins:
    print("[start] starting keygen bot + dashboard", flush=True)
    threading.Thread(target=keygen_forever, daemon=True).start()
else:
    print("[start] KEYGEN_BOT_TOKEN / KEYGEN_ADMINS missing in .env - starting the dashboard WITHOUT the keygen bot", flush=True)

try:  # the dashboard is the main process: if it stops, everything stops (Railway then restarts it)
    sys.exit(subprocess.call([sys.executable, "dashboard.py"], cwd=str(HERE)))
except KeyboardInterrupt:
    sys.exit(0)

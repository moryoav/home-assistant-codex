"""Install the shipped frontend and dependencies imported by HA service schemas."""
import importlib.resources
import json
import subprocess
import sys

domains = (
    "frontend", "camera", "conversation", "tts", "ffmpeg", "media_player",
    "calendar", "todo", "recorder", "assist_pipeline",
)
requirements = set()
for domain in domains:
    manifest = importlib.resources.files("homeassistant").joinpath("components", domain, "manifest.json")
    requirements.update(json.loads(manifest.read_text()).get("requirements", []))
subprocess.check_call([sys.executable, "-m", "pip", "install", *sorted(requirements)])

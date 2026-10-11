"""Docker healthcheck: exit 0 if the tracker is running and healthy, 1 otherwise."""
import json
import os
import sys
import time

path = os.environ.get("HEALTH_FILE", "data/health.json")
try:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
except (OSError, ValueError) as e:
    print(f"no status file: {e}")
    sys.exit(1)

age = time.time() - d.get("updated_at", 0)
if age > 90:
    print(f"status file is {int(age)}s old, the tracker has stopped reporting")
    sys.exit(1)
if not d.get("ok"):
    print("; ".join(d.get("problems") or ["unhealthy"]))
    sys.exit(1)
print("ok")

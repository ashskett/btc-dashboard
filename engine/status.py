import json
from datetime import datetime

STATUS_FILE = "engine_status.json"


def write_status(data):
    """
    Write engine status to a JSON file for the dashboard
    """

    data["timestamp"] = datetime.utcnow().isoformat()

    with open(STATUS_FILE, "w") as f:
        json.dump(data, f, indent=2)
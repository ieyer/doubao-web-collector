import json
import os


class SlotConfig:
    def __init__(self, slot_id, profile_dir, enabled=True):
        self.slot_id = slot_id
        self.profile_dir = profile_dir
        self.enabled = enabled


class Config:
    def __init__(self, path=None):
        path = path or os.environ.get("COLLECTOR_CONFIG", "config.json")
        raw = {}
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        self.host = raw.get("host", "0.0.0.0")
        self.port = int(raw.get("port", 8790))
        self.token = os.environ.get("COLLECTOR_TOKEN") or raw.get("token", "")
        self.data_dir = raw.get("data_dir", "data")
        self.driver = os.environ.get("COLLECTOR_DRIVER") or raw.get("driver", "doubao")
        self.queue_limit = int(raw.get("queue_limit", 100))
        self.run_timeout_sec = int(raw.get("run_timeout_sec", 360))
        self.assets_retention_days = int(raw.get("assets_retention_days", 30))
        self.slots = []
        for s in raw.get("slots", []):
            self.slots.append(
                SlotConfig(s["slot_id"], s["profile_dir"], bool(s.get("enabled", True)))
            )
        if not self.slots:
            self.slots = [SlotConfig("slot-1", "profiles/slot-1", True)]

    def db_path(self):
        return os.path.join(self.data_dir, "collector.db")

    def assets_dir(self):
        return os.path.join(self.data_dir, "assets")

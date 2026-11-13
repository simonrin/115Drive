"""Bounded completed-operation history, separate from sync recovery state."""
import datetime as dt
import hashlib
import json
import threading
from runtime_json import write_snapshot


class TaskActivity:
    def __init__(self, directory, limit=100):
        self.path = directory/'activity.json'
        self.limit = limit
        self.lock = threading.Lock()
        try:
            rows = json.loads(self.path.read_text(encoding='utf-8'))
            self.rows = [row for row in rows if isinstance(row, dict) and isinstance(row.get('id'), str) and row['id']][-limit:] if isinstance(rows, list) else []
        except (OSError, ValueError):
            self.rows = []

    def completed(self, action, name, remote=None, *, operation=None, size=0):
        remote = remote or {}
        identity = operation or hashlib.sha256(json.dumps(
            [action, name, remote], sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        with self.lock:
            if any(row.get('id') == identity for row in self.rows): return
            self.rows.append(dict(id=identity, time=dt.datetime.now().astimezone().isoformat(),
                                  name=name, action=action, size=size or remote.get('size', 0)))
            self.rows = self.rows[-self.limit:]
            write_snapshot(self.path, self.rows)

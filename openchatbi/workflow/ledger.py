"""Request-scoped, append-only records for independent stage evaluation."""

import json
import re
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

_lock = threading.RLock()


def redact(value):
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if re.search(r"api[_-]?key|password|secret|access[_-]?token|authorization", str(key), re.I)
                and not (key == "authorization" and isinstance(item, dict))
                else redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


class RunLedger:
    def __init__(self, report_directory, request_id):
        if len(request_id) != 32 or any(char not in "0123456789abcdef" for char in request_id):
            raise ValueError("Invalid ledger request ID")
        self.request_id = request_id
        self.path = Path(report_directory) / ".runs" / request_id / "events.jsonl"

    def record(self, *, task_id, domain, node, inputs, outputs, status="ok", duration_ms=0, version=1):
        entry = {
            "event_id": uuid.uuid4().hex,
            "request_id": self.request_id,
            "task_id": task_id,
            "domain": domain,
            "node": node,
            "version": version,
            "time": datetime.now(UTC).isoformat(),
            "status": status,
            "inputs": redact(inputs),
            "outputs": redact(outputs),
            "duration_ms": duration_ms,
        }
        with _lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return entry

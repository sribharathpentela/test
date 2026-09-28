import json
import os
from datetime import datetime, timezone
from typing import Dict, Any, List
from pydantic import BaseModel


class GapEntry(BaseModel):
    ticket_id: str
    requested_resource_type: str
    environment: str
    reason: str
    timestamp: str


class GapRegisterLogger:
    GAP_FILE_PATH = os.path.join(
        os.path.dirname(__file__), "gap_entries.json"
    )

    @classmethod
    def log_unsupported_resource(
        cls,
        ticket_id: str,
        requested_resource_type: str,
        environment: str = "unknown",
        reason: str = "Resource type not available in Central Module Registry",
    ) -> GapEntry:
        entry = GapEntry(
            ticket_id=ticket_id,
            requested_resource_type=requested_resource_type,
            environment=environment,
            reason=reason,
            timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )

        entries = cls.get_all_entries()
        entries.append(entry.model_dump())

        os.makedirs(os.path.dirname(cls.GAP_FILE_PATH), exist_ok=True)
        with open(cls.GAP_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2)

        return entry

    @classmethod
    def get_all_entries(cls) -> List[Dict[str, Any]]:
        if not os.path.exists(cls.GAP_FILE_PATH):
            return []
        try:
            with open(cls.GAP_FILE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

"""Append-only audit log of decisions: request, full agent trace and code revision.

The case requires that inputs, agent assessments and the final recommendation
are stored for audit.  Each decision is one JSON file under
``storage/<dataset>/decisions``; files are never rewritten.  Set
``OILCODE_DECISION_LOG=0`` to disable writing (e.g. read-only storage).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ROOT

RECORD_ID = re.compile(r"^[0-9]{8}T[0-9]{12}_[0-9a-f]{10}$")


def _revision() -> str:
    path = ROOT / "build_revision.txt"
    return path.read_text(encoding="utf-8").strip() if path.exists() else "development"


def _folder(directory: Path) -> Path:
    return directory / "decisions"


def record(directory: Path, request: dict[str, Any], decision: dict[str, Any]) -> str | None:
    if os.environ.get("OILCODE_DECISION_LOG", "1") == "0":
        return None
    created = datetime.now()
    payload = json.dumps({"request": request, "decision": decision}, ensure_ascii=False, sort_keys=True, default=str)
    record_id = f"{created:%Y%m%dT%H%M%S%f}_{hashlib.sha256(payload.encode()).hexdigest()[:10]}"
    folder = _folder(directory)
    folder.mkdir(parents=True, exist_ok=True)
    body = {"id": record_id, "created_at": created.isoformat(), "revision": _revision(),
            "request": request, "decision": decision}
    temporary = folder / f"{record_id}.tmp"
    temporary.write_text(json.dumps(body, ensure_ascii=False, default=str), encoding="utf-8")
    temporary.replace(folder / f"{record_id}.json")
    return record_id


def get_record(directory: Path, record_id: str) -> dict[str, Any]:
    if not RECORD_ID.match(record_id or ""):
        raise KeyError(record_id)
    path = _folder(directory) / f"{record_id}.json"
    if not path.exists():
        raise KeyError(record_id)
    return json.loads(path.read_text(encoding="utf-8"))


def list_records(directory: Path, limit: int = 50) -> list[dict[str, Any]]:
    folder = _folder(directory)
    if not folder.exists():
        return []
    items = []
    for path in sorted(folder.glob("*.json"), reverse=True)[:limit]:
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        decision = body.get("decision") or {}
        items.append({
            "id": body.get("id"), "created_at": body.get("created_at"), "revision": body.get("revision"),
            "at": decision.get("at"), "status": decision.get("status"),
            "selected_candidate": decision.get("selected_candidate"),
            "explanation_source": (decision.get("explanation") or {}).get("source"),
        })
    return items

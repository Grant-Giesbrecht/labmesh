
from __future__ import annotations
import json, os
from typing import Any, Dict

def dumps(obj: Dict[str, Any]) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")

def loads(b: bytes) -> Dict[str, Any]:
    return json.loads(b.decode("utf-8"))

def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


from __future__ import annotations
import json, os
from typing import Any, Dict
try:
	import tomllib as toml  # py311+
except Exception:
	import tomli as toml

def read_toml_config(filename:str):
	
	cfg = None
	with open(filename, "rb") as f:
		cfg = toml.load(f)
	
	return cfg

def ensure_windows_selector_loop():
	import sys, asyncio
	if sys.platform.startswith("win") and not isinstance(
		asyncio.get_event_loop_policy(), asyncio.WindowsSelectorEventLoopPolicy
	):
		asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

def dumps(obj: Dict[str, Any]) -> bytes:
	return json.dumps(obj, separators=(",", ":")).encode("utf-8")

def loads(b: bytes) -> Dict[str, Any]:
	return json.loads(b.decode("utf-8"))

def env_int(name: str, default: int) -> int:
	try:
		return int(os.environ.get(name, str(default)))
	except Exception:
		return default

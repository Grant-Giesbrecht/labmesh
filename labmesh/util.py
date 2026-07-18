
from __future__ import annotations
import json, os, hmac, getpass
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

def network_password() -> str:
	""" Shared mesh password read from LMH_NETWORK_PASSWORD.

	Every node reads this the same way CURVE keys are read from env vars.
	Empty string means auth is disabled (matches CURVE's opt-in-if-set behavior).
	"""
	return os.environ.get("LMH_NETWORK_PASSWORD", "")

def check_password(msg: Dict[str, Any], expected: str) -> bool:
	""" Constant-time check of a message's declared password against the expected one. """
	return hmac.compare_digest(str(msg.get("password") or ""), str(expected or ""))

def prompt_network_password(confirm: bool = False) -> str:
	""" Interactively prompt for the mesh password (used at broker boot) and store it
	in LMH_NETWORK_PASSWORD for the rest of this process. Does nothing if the env var
	is already set (e.g. for scripted/non-interactive deployments).
	"""
	pw = os.environ.get("LMH_NETWORK_PASSWORD")
	if pw is not None:
		return pw  # explicitly set (even "" to disable auth) - don't prompt, so boot stays non-interactive
	pw = getpass.getpass("Set labmesh network password (all nodes must use this to connect; leave blank to disable): ")
	if confirm and pw:
		pw2 = getpass.getpass("Confirm password: ")
		if pw != pw2:
			raise SystemExit("Passwords did not match.")
	os.environ["LMH_NETWORK_PASSWORD"] = pw
	return pw

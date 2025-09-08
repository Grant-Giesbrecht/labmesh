# security.py — lmh-gen-curve helper
from __future__ import annotations
import argparse
import json
import zmq  # pip install pyzmq

def generate_curve_keypair():
	public, secret = zmq.curve_keypair()
	return {"public": public.decode(), "secret": secret.decode()}

def main(argv=None):
	p = argparse.ArgumentParser(description="Generate ZeroMQ CURVE keypair")
	parser.add_argument("--format", choices=["env", "json", "toml"], default="env",
				   help="Output format: env (default), json, or toml")
	parser.add_argument("--section", default="curve.server",
				   help="For --format toml: table path (e.g., broker.curve.server)")
	args = parser.parse_args(argv)
	
	pair = generate_curve_keypair()
	
	if args.format == "json":
		print(json.dumps(pair))
	elif args.format == "toml":
		print(f"[{args.section}]")
		print(f'public = "{pair["public"]}"')
		print(f'secret = "{pair["secret"]}"')
	else:  # env
		print("ZMQ_PUBLICKEY=\"" + pair["public"]+"\"")
		print("ZMQ_SECRETKEY=\"" + pair["secret"]+"\"")

if __name__ == "__main__":
	main()

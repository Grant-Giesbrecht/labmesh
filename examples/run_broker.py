
import asyncio
from labmesh import DirectoryBroker
import argparse
from util import read_toml_config

# Create a parser
parser = argparse.ArgumentParser()
p.add_argument("--toml", help="Set TOML configuration file", default="labmesh.toml")
args = p.parse_args(argv)

# Read TOML file
toml_broker = read_toml_config("labmesh.toml")['broker']

if __name__ == "__main__":
    broker = DirectoryBroker(toml_broker['rpc_bind'], toml_broker['xsub_bind'], toml_broker['xpub_bind'])
    asyncio.run(broker.serve())


import asyncio
from labmesh import DataBank
from util import read_toml_config

# Create a parser
parser = argparse.ArgumentParser()
p.add_argument("--toml", help="Set TOML configuration file", default="labmesh.toml")
p.add_argument("--bank_id", help="Bank ID to use on the network.", default="bank-0")
args = p.parse_args(argv)

# Read TOML file
toml_data = read_toml_config("labmesh.toml")
toml_bank = toml_data['bank']

if __name__ == "__main__":
	bank = DataBank(ingest_bind=toml_bank['ingest_bind'], retrieve_bind=toml_bank['retrieve_bind'], data_dir=toml_bank['default_data_dir'], broker_rpc=toml_bank['broker_rpc'], broker_xsub=toml_bank['broker_xsub'], bank_id=args.bank_id, heartbeat_sec=toml_bank['heartbeat_seconds'])
	asyncio.run(bank.serve())

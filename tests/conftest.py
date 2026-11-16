import os

# Tests run against the synthetic CSV export, never a live CRM, regardless of .env.
os.environ["LORE_CRM"] = "csv"

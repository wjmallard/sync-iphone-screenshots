"""Load project config from config.yaml."""

from pathlib import Path

import yaml

_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config.yaml"

with open(_CONFIG_PATH) as f:
    _raw = yaml.safe_load(f)

OUTPUT_DIR = Path(_raw["output_dir"]).expanduser()
WORKERS = _raw["workers"]
DB_NAME = _raw["db_name"]
COMMIT_BATCH_SIZE = _raw["commit_batch_size"]

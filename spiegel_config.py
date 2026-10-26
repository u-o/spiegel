"""Where the Spiegel corpus lives on this machine.

Every script gets its data folder from here. Lookup order:
  1. the SPIEGEL_DATA environment variable (handy for one-off runs): either
     a path, or a name like "sample" that picks sample_dir from config.toml,
  2. data_dir in config.toml (git-ignored; copy config.example.toml),
  3. downloads/ inside the repository.
"""
import os
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
CONFIG_FILE = REPO_ROOT / "config.toml"
RESULTS_DIR = REPO_ROOT / "data"


def _resolve(value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def _data_dir():
    config = {}
    if CONFIG_FILE.exists():
        with CONFIG_FILE.open("rb") as f:
            config = tomllib.load(f)
    override = os.environ.get("SPIEGEL_DATA")
    if override:
        return _resolve(config.get(f"{override}_dir", override))
    if config.get("data_dir"):
        return _resolve(config["data_dir"])
    return REPO_ROOT / "downloads"


DATA_DIR = _data_dir()

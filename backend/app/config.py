from __future__ import annotations

import os
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _config_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = APP_ROOT / path
    return path


DOTENV = _load_dotenv(APP_ROOT / ".env")
LOCAL_REFERENCE_CONFIG_ROOT = APP_ROOT / "backend" / "reference_topologies"
REFERENCE_CONFIG_ROOT = _config_path(
    os.environ.get("REFERENCE_CONFIG_ROOT")
    or DOTENV.get("REFERENCE_CONFIG_ROOT", "")
    or LOCAL_REFERENCE_CONFIG_ROOT
)
INVENTORY_PATH = APP_ROOT / "backend" / "data" / "hardware_inventory.json"
OUTPUTS_ROOT = APP_ROOT / "outputs"
LAB_NAVIGATOR_BASE_URL = (
    os.environ.get("LAB_NAVIGATOR_BASE_URL")
    or DOTENV.get("LAB_NAVIGATOR_BASE_URL", "")
    or "https://lab-navigator.velo.maa.aristanetworks.com"
)
LAB_NAVIGATOR_API_KEY = os.environ.get("LN_PROD_API_KEY") or DOTENV.get("LN_PROD_API_KEY", "")

REFERENCE_TOPOLOGIES = [
    "1-site",
    "2-site",
    "3-site",
    "3-site/spirent",
    "3-site-ipv6",
    "3-site-scale",
    "3-site-scale/spirent",
    "3-site-vnf",
    "5-site",
    "5-site-mpg",
    "5-site-mpg-gre",
    "5-site-ipv6",
    "5-site-cluster",
    "5-site-cluster/hitless",
    "5-site-cluster/spirent",
    "5-site-eos",
    "7-site",
]

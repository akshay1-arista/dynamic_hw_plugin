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


def _optional_config_path(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return _config_path(text)


def _config_bool(value: str | bool | None, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


DOTENV = _load_dotenv(APP_ROOT / ".env")
LOCAL_REFERENCE_CONFIG_ROOT = APP_ROOT / "backend" / "reference_topologies"
REFERENCE_CONFIG_ROOT = _config_path(
    os.environ.get("REFERENCE_CONFIG_ROOT")
    or DOTENV.get("REFERENCE_CONFIG_ROOT", "")
    or LOCAL_REFERENCE_CONFIG_ROOT
)
INVENTORY_PATH = APP_ROOT / "backend" / "data" / "hardware_inventory.json"
INVENTORY_STATE_PATH = _config_path(
    os.environ.get("INVENTORY_STATE_PATH")
    or DOTENV.get("INVENTORY_STATE_PATH", "")
    or (APP_ROOT / "backend" / "data" / "hardware_inventory.local.json")
)
OUTPUTS_ROOT = APP_ROOT / "outputs"
HAPY_REPO_ROOT = _optional_config_path(
    os.environ.get("HAPY_REPO_ROOT") or DOTENV.get("HAPY_REPO_ROOT", "")
)
HAPY_TESTBED_CONFIG_ROOT = _optional_config_path(
    os.environ.get("HAPY_TESTBED_CONFIG_ROOT")
    or DOTENV.get("HAPY_TESTBED_CONFIG_ROOT", "")
) or (HAPY_REPO_ROOT / "hapy" / "hapy" / "testbed" / "configs" if HAPY_REPO_ROOT else None)
HAPY_GERRIT_REMOTE_NAME = (
    os.environ.get("HAPY_GERRIT_REMOTE_NAME")
    or DOTENV.get("HAPY_GERRIT_REMOTE_NAME", "")
    or "origin"
)
HAPY_GIT_USER_NAME = (
    os.environ.get("HAPY_GIT_USER_NAME")
    or DOTENV.get("HAPY_GIT_USER_NAME", "")
    or ""
).strip()
HAPY_GIT_USER_EMAIL = (
    os.environ.get("HAPY_GIT_USER_EMAIL")
    or DOTENV.get("HAPY_GIT_USER_EMAIL", "")
    or ""
).strip()
HAPY_PRIVATE_BRANCH_REGISTRY_PATH = _config_path(
    os.environ.get("HAPY_PRIVATE_BRANCH_REGISTRY_PATH")
    or DOTENV.get("HAPY_PRIVATE_BRANCH_REGISTRY_PATH", "")
    or (APP_ROOT / "backend" / "data" / "hapy_private_branches.json")
)
AUDIT_LOG_PATH = _config_path(
    os.environ.get("AUDIT_LOG_PATH")
    or DOTENV.get("AUDIT_LOG_PATH", "")
    or (APP_ROOT / "backend" / "data" / "audit_log.json")
)
HAPY_BASE_BRANCHES = [
    "release_5.2",
    "release_6.1",
    "release_6.4",
    "release_7.0",
    "master",
]
LAB_NAVIGATOR_BASE_URL = (
    os.environ.get("LAB_NAVIGATOR_BASE_URL")
    or DOTENV.get("LAB_NAVIGATOR_BASE_URL", "")
    or "https://lab-navigator.velo.maa.aristanetworks.com"
)
LAB_NAVIGATOR_API_KEY = os.environ.get("LN_PROD_API_KEY") or DOTENV.get("LN_PROD_API_KEY", "")
LAB_NAVIGATOR_CA_BUNDLE = _optional_config_path(
    os.environ.get("LAB_NAVIGATOR_CA_BUNDLE") or DOTENV.get("LAB_NAVIGATOR_CA_BUNDLE", "")
)
LAB_NAVIGATOR_TLS_VERIFY = _config_bool(
    os.environ.get("LAB_NAVIGATOR_TLS_VERIFY") or DOTENV.get("LAB_NAVIGATOR_TLS_VERIFY"),
    True,
)

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

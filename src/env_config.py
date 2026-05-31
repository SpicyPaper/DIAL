"""
Small .env helpers used by CLI entry points.
"""

import os
from pathlib import Path


def project_env_path() -> Path:
    return Path(__file__).resolve().parents[1] / ".env"


def load_project_env() -> None:
    path = project_env_path()
    if not path.exists():
        raise RuntimeError(f"Missing .env file at {path}.")

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise RuntimeError(f"Missing {name} in .env")
    return value


def env_float(name: str) -> float:
    return float(require_env(name))


def env_int(name: str) -> int:
    return int(require_env(name))


def env_bool(name: str) -> bool:
    return require_env(name).lower() in {"1", "true", "yes", "on"}

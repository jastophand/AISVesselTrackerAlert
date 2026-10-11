"""Config loading helpers (no heavy dependencies so tests can import them)."""
import os
import re

import yaml

_VAR = re.compile(r"^\$\{(\w+)(?::-(.*))?\}$")


def load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def expand_env(obj):
    """Replace "${NAME}" or "${NAME:-default}" values with environment variables.

    Keeps secrets out of files and lets Portainer's environment variable
    screen drive the whole config. Values come back as strings.
    """
    if isinstance(obj, dict):
        return {k: expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env(v) for v in obj]
    if isinstance(obj, str):
        m = _VAR.match(obj)
        if m:
            val = os.environ.get(m.group(1))
            if val:
                return val
            return m.group(2) if m.group(2) is not None else ""
    return obj

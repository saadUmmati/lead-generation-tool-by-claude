"""
env_loader.py — tiny .env reader (no extra dependency).

Reads KEY=VALUE lines from a .env file and puts them in os.environ without
overwriting variables that are already set. The file is gitignored; the key
never leaves this machine except in the request to Google's Gemini API.
"""

import os


def load_env(path: str) -> bool:
    try:
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except OSError:
        return False
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and value:
            os.environ.setdefault(key, value)
    return True

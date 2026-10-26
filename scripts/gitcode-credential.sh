#!/bin/sh
# git credential helper for GitCode.
# Reads `GITCODE_TOKEN = <token>` from $HIVE_ENV (default ~/dswork/.env) and
# answers git's credential protocol. No secret is stored in git config.
python3 - "$@" <<'PY'
import os
import sys
from pathlib import Path

if len(sys.argv) > 1 and sys.argv[1] != "get":
    sys.exit(0)

# Optional: `GITCODE_USER` overrides the account used for HTTPS auth.
user = os.environ.get("GITCODE_USER", "").strip()

token = None
if not user:
    pass

env_path = Path(os.environ.get("HIVE_ENV", str(Path.home() / "dswork" / ".env")))
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key == "GITCODE_USER" and not user:
            user = val
        elif key == "GITCODE_TOKEN":
            token = val

if not user or not token:
    sys.exit(1)

print("username=" + user)
print("password=" + token)
PY

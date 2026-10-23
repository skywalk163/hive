#!/bin/sh
# git credential helper for the local Gitea.
# Reads `gitea = http://user:token@host:port` from $HIVE_ENV (default ~/dswork/.env)
# and answers git's credential protocol. No secret is stored in git config.
python3 - "$@" <<'PY'
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

if len(sys.argv) > 1 and sys.argv[1] != "get":
    sys.exit(0)

env_path = Path(os.environ.get("HIVE_ENV", str(Path.home() / "dswork" / ".env")))
if not env_path.exists():
    sys.exit(1)

value = None
for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
    line = line.strip()
    if line.startswith("#") or "=" not in line:
        continue
    key, val = line.split("=", 1)
    if key.strip() == "gitea":
        value = val.strip().strip('"').strip("'")
        break

if not value:
    sys.exit(1)

u = urlparse(value)
if not u.username or not u.password:
    sys.exit(1)

print("username=" + unquote(u.username))
print("password=" + unquote(u.password))
PY

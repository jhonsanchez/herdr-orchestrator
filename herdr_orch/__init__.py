import sys

if sys.version_info < (3, 11):  # tomllib; macOS's /usr/bin/python3 is 3.9
    raise SystemExit("herdr-orchestrator needs Python 3.11 or newer")

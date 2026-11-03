#!/usr/bin/env bash
set -euo pipefail

TEDD_REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
  TEDD_PYTHON=python3
elif command -v python >/dev/null 2>&1 && python -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
  TEDD_PYTHON=python
else
  printf 'Python 3.10 or newer is required to verify Tedd.\n' >&2
  exit 69
fi

exec "$TEDD_PYTHON" "$TEDD_REPOSITORY_ROOT/scripts/verify_release.py" --require-bash "$@"

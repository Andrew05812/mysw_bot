#!/usr/bin/env bash
# Run the mysw_bot test suite in an ephemeral container based on the production
# image (so it has the real runtime deps) WITHOUT touching the production bot.
# Usage: ./scripts/test.sh [mybot:image-tag]
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE="${1:-mybot:81bcd03}"
docker run --rm --entrypoint bash \
  -v "$PWD:/workspace" \
  "$IMAGE" \
  -c "pip install -q pytest >/dev/null 2>&1; cd /workspace && python -m pytest tests -q"

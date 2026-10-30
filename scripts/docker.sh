#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TAG="${1:-dev}"
IMAGE="${STRIX_IMAGE:-strix-sandbox:$TAG}"

# Send only the Dockerfile's source inputs, not credentials, runs, or dependencies.
BUILD_CONTEXT="$(mktemp -d)"
trap 'rm -rf -- "$BUILD_CONTEXT"' EXIT
mkdir -p "$BUILD_CONTEXT/containers" "$BUILD_CONTEXT/strix/tools/proxy"
cp "$PROJECT_ROOT/containers/Dockerfile" "$PROJECT_ROOT/containers/docker-entrypoint.sh" \
  "$BUILD_CONTEXT/containers/"
cp "$PROJECT_ROOT/strix/tools/proxy/caido_api.py" "$BUILD_CONTEXT/strix/tools/proxy/"

echo "Building $IMAGE ..."
docker build \
  -f "$BUILD_CONTEXT/containers/Dockerfile" \
  -t "$IMAGE" \
  "$BUILD_CONTEXT"

echo "Done: $IMAGE"

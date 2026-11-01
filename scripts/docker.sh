#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TAG="${1:-dev}"
IMAGE="${NATSUHAT_IMAGE:-natsuhat-sandbox:$TAG}"

# Send only the Dockerfile's source inputs, not credentials, runs, or dependencies.
BUILD_CONTEXT="$(mktemp -d)"
trap 'rm -rf -- "$BUILD_CONTEXT"' EXIT
mkdir -p "$BUILD_CONTEXT/containers" "$BUILD_CONTEXT/natsuhat/tools/proxy"
cp "$PROJECT_ROOT/containers/Dockerfile" "$PROJECT_ROOT/containers/docker-entrypoint.sh" \
  "$BUILD_CONTEXT/containers/"
cp "$PROJECT_ROOT/natsuhat/tools/proxy/caido_api.py" "$BUILD_CONTEXT/natsuhat/tools/proxy/"

echo "Building $IMAGE ..."
docker build \
  -f "$BUILD_CONTEXT/containers/Dockerfile" \
  -t "$IMAGE" \
  "$BUILD_CONTEXT"

echo "Done: $IMAGE"

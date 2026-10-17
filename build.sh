#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

IMAGE=hermes-doro-hardened

extra=()
if [ "${1:-}" = "--pull" ]; then
  extra+=(--pull) # force-refresh the base image (nousresearch/hermes-agent:latest)
fi

echo "==> Building image $IMAGE"
docker build "${extra[@]}" -t "$IMAGE" .

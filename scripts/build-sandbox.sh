#!/usr/bin/env bash
# Build the test sandbox image. Tag must match REPOLENS_SANDBOX_IMAGE (default below).
set -euo pipefail
cd "$(dirname "$0")/.."
tag="${1:-repolens-sandbox:py3.12-1}"
docker build --pull=false -t "$tag" infra/docker/sandbox
docker image inspect "$tag" --format 'built {{.Id}} ({{.Size}} bytes)'

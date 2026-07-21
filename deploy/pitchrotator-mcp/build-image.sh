#!/usr/bin/env bash
# Build the reviewed PitchRotator MCP source plus the measured RedPill/GLM-5.2 overlay.
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
SOURCE_REPO="${SOURCE_REPO:-/tmp/pitchrotator-issue35}"
IMAGE_REPO="${IMAGE_REPO:-ghcr.io/attestmesh/pitchrotator-mcp}"
SOURCE_COMMIT=66b5495b0ea0695ef6d2a35969d444da4f680a52
SOURCE_TREE=30ef21a38034bf1d1f7001445a6feea89a424cb3
SOURCE_ARCHIVE_SHA256=57aa6a29108cdaa5a46cd6d12b962c7c01c8ca824882b77f16767ea395843e1d
LOCKFILE_SHA256=3a3e75e10c0ebb9ed132cf93fd4641cc3c8d043c55e4a443ac42f32c25d73342
OVERLAY_SHA256=baa89e6b4c2eaf04c1fd81b7c4c0a026c68e1de8c7c5ec5cfa4275b733807559

for tool in git sha256sum tar patch docker jq; do
  command -v "$tool" >/dev/null || { echo "missing required tool: $tool" >&2; exit 1; }
done
[ -d "$SOURCE_REPO/.git" ] || { echo "missing private source checkout: $SOURCE_REPO" >&2; exit 1; }

actual_tree="$(git -C "$SOURCE_REPO" rev-parse "$SOURCE_COMMIT^{tree}")"
[ "$actual_tree" = "$SOURCE_TREE" ] || { echo "source tree mismatch" >&2; exit 1; }

build_root="$(mktemp -d "${TMPDIR:-/tmp}/pitchrotator-build.XXXXXX")"
archive="$build_root/source.tar.gz"
cleanup() { rm -rf "$build_root"; }
trap cleanup EXIT

git -C "$SOURCE_REPO" archive --format=tar.gz \
  --prefix=Pitch-Rotator-66b5495/ "$SOURCE_COMMIT" >"$archive"
echo "$SOURCE_ARCHIVE_SHA256  $archive" | sha256sum -c -
tar -xzf "$archive" -C "$build_root"
context="$build_root/Pitch-Rotator-66b5495/mcp-server"
echo "$LOCKFILE_SHA256  $context/package-lock.json" | sha256sum -c -
echo "$OVERLAY_SHA256  $HERE/model-redpill-glm-5.2.patch" | sha256sum -c -
patch -d "$build_root/Pitch-Rotator-66b5495" -p1 <"$HERE/model-redpill-glm-5.2.patch"

overlay_commit="$(git -C "$ROOT" rev-parse HEAD)"
overlay_hash="$(sha256sum "$HERE/model-redpill-glm-5.2.patch" "$HERE/Dockerfile" | sha256sum | cut -c1-12)"
tag="${IMAGE_TAG:-$SOURCE_COMMIT-redpill-glm-5.2-$overlay_hash}"
image="$IMAGE_REPO:$tag"

docker buildx build --platform linux/amd64 --load \
  --file "$HERE/Dockerfile" \
  --build-arg "SOURCE_COMMIT=$SOURCE_COMMIT" \
  --build-arg "SOURCE_TREE=$SOURCE_TREE" \
  --build-arg "OVERLAY_COMMIT=$overlay_commit" \
  --tag "$image" "$context"

docker image inspect "$image" --format '{{json .Config.Labels}}' | jq -e \
  --arg source "$SOURCE_COMMIT" --arg tree "$SOURCE_TREE" --arg overlay "$overlay_commit" \
  '."org.opencontainers.image.revision" == $source and
   ."io.attestmesh.pitchrotator.source-tree" == $tree and
   ."io.attestmesh.pitchrotator.overlay-commit" == $overlay and
   ."io.attestmesh.pitchrotator.provider" == "redpill" and
   ."io.attestmesh.pitchrotator.model" == "z-ai/glm-5.2"' >/dev/null

printf '%s\n' "$image"

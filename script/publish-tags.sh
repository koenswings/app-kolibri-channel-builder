#!/usr/bin/env bash
# Which image tags may CI push? (never overwrite an existing release tag in ghcr)
# Usage: publish-tags.sh <image repo> <candidate tag>...   prints "repo:tag" lines to push.
# `main` is the moving branch tag and is always pushed. Any other tag is pushed only if it does not
# exist yet. Existence check: $TAG_EXISTS_CMD <repo:tag> (default: docker buildx imagetools inspect).
set -euo pipefail
repo=$1; shift
exists() { ${TAG_EXISTS_CMD:-docker buildx imagetools inspect} "$1" >/dev/null 2>&1; }
for t in "$@"; do
  [ -n "$t" ] || continue
  if [ "$t" = main ]; then echo "$repo:$t"; continue; fi
  if exists "$repo:$t"; then echo "skip: $repo:$t already exists in the registry; release tags are never overwritten" >&2
  else echo "$repo:$t"; fi
done

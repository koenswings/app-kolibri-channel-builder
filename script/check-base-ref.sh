#!/bin/sh
# script/check-base-ref.sh — idea#403: compare compose.yaml with the previous release with agent-app-dev
# check-compose --base-ref. A stateful image (MariaDB, MySQL, Postgres, Mongo, Redis) that moves to a new
# series needs an App major bump (agent-app-dev docs/app-bundle-integrity.md "Stateful images need an
# App major").
#
#   AAD_CHECK_COMPOSE=<agent-app-dev>/script/check-compose.mjs script/check-base-ref.sh
#   script/check-base-ref.sh --print-tag      # only print the previous release tag (or nothing)
#
# Run it before tagging, in a full clone with its tags (CI: fetch-depth: 0).
# Previous release tag: the highest release tag <major>.<minor>[-<upstream>] that is an ancestor of
# HEAD and does not point at HEAD itself.
#  - A shallow clone fails (git fetch --unshallow --tags).
#  - A clone with no release tags at all fails (tags not fetched), unless CHECK_BASE_REF_FIRST_RELEASE=1
#    is set for a real first release.
#  - Release tags present but none of them an ancestor of HEAD (other than HEAD's own): first release,
#    exit 0 with a note.
set -eu
cd "$(dirname "$0")/.."
RELEASE_RE='^[0-9]+\.[0-9]+(-[A-Za-z0-9._]+)?$'
if [ "$(git rev-parse --is-shallow-repository)" = true ]; then
	echo "check-base-ref: shallow clone; run git fetch --unshallow --tags (CI: actions/checkout fetch-depth: 0)" >&2
	exit 1
fi
all=$(git tag -l)
if ! printf '%s\n' "$all" | grep -Eq "$RELEASE_RE"; then
	if [ "${CHECK_BASE_REF_FIRST_RELEASE:-}" = 1 ]; then
		[ "${1:-}" = --print-tag ] || echo "check-base-ref: CHECK_BASE_REF_FIRST_RELEASE=1 and no release tags (first release); stateful-image check skipped"
		exit 0
	fi
	echo "no release tags in this clone: git fetch --tags; for a real first release set CHECK_BASE_REF_FIRST_RELEASE=1" >&2
	exit 1
fi
here=$(git tag --points-at HEAD)
tags=$(git tag --merged HEAD)
prev=$(printf '%s\n' "$tags" \
	| grep -E "$RELEASE_RE" \
	| { if [ -n "$here" ]; then grep -vxF "$here"; else cat; fi; } \
	| sort -t. -k1,1n -k2,2n | tail -n 1)
if [ "${1:-}" = --print-tag ]; then
	[ -z "$prev" ] || echo "$prev"
	exit 0
fi
if [ -z "$prev" ]; then
	echo "check-base-ref: no earlier release tag is an ancestor of HEAD (first release); stateful-image check skipped"
	exit 0
fi
: "${AAD_CHECK_COMPOSE:?set AAD_CHECK_COMPOSE to agent-app-dev script/check-compose.mjs}"
echo "check-base-ref: base $prev"
exec node "$AAD_CHECK_COMPOSE" --base-ref "$prev" compose.yaml

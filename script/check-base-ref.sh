#!/bin/sh
# script/check-base-ref.sh — idea#403: compare compose.yaml with the previous release with agent-app-dev
# check-compose --base-ref. A stateful image (MariaDB, MySQL, Postgres, Mongo, Redis) that moves to a new
# series needs an App major bump (agent-app-dev docs/app-bundle-integrity.md "Stateful images need an
# App major").
#
#   AAD_CHECK_COMPOSE=<agent-app-dev>/script/check-compose.mjs script/check-base-ref.sh
#   script/check-base-ref.sh --print-tag      # only print the previous release tag (or nothing)
#
# Previous release tag: the highest release tag <major>.<minor>[-<upstream>] that is an ancestor of
# HEAD and does not point at HEAD itself. No such tag (a first release): exit 0 with a note.
# A shallow clone fails: tags and history are needed (CI: fetch-depth: 0, or git fetch --unshallow --tags).
set -eu
cd "$(dirname "$0")/.."
if [ "$(git rev-parse --is-shallow-repository)" = true ]; then
	echo "check-base-ref: shallow clone; run git fetch --unshallow --tags (CI: actions/checkout fetch-depth: 0)" >&2
	exit 1
fi
here=$(git tag --points-at HEAD)
prev=$(git tag --merged HEAD \
	| grep -E '^[0-9]+\.[0-9]+(-[A-Za-z0-9._]+)?$' \
	| { if [ -n "$here" ]; then grep -vxF "$here"; else cat; fi; } \
	| sort -t. -k1,1n -k2,2n | tail -n 1) || prev=
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

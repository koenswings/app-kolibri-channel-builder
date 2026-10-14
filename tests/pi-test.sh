#!/usr/bin/env bash
# Channel builder on-Pi acceptance test.
# Usage: tests/pi-test.sh <free-port> [evidence-dir]   (uses <port>, <port>+1, <port>+2)
# Exit 0 = pass. Writes an evidence folder (default ./pi-evidence-<host>-<UTC>).
# Steps: build image (test tag) -> compose up with the real compose.yaml (mem_limit 300m)
# -> generate sample teacher files -> POST /build -> throwaway Kolibri 0.15.5 container
# (from the builder image's own kolibri==0.15.5; never the Pi's live Kolibri) imports the
# channel via `importchannel/importcontent network` -> all resources available -> fetch the
# image resource's HTML5 page over HTTP from that Kolibri. Cleans up containers, network,
# volumes, temp dirs and images the test created/pulled (KEEP_IMAGES=1 keeps them).
# Refuses idea02 / wizardly-hugle. Requires arm64 unless ALLOW_NON_ARM64=1 (dry runs).
set -uo pipefail
HOST=$(hostname -s 2>/dev/null || hostname)
case "${HOST,,}" in idea02|wizardly-hugle) echo "REFUSED: $HOST is a live pool host; use a dev Pi." >&2; exit 2;; esac
PORT=${1:-}
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || (( PORT<1024 || PORT>65535 )); then echo "usage: $0 <free-port 1024-65535> [evidence-dir]" >&2; exit 2; fi
# Throwaway Kolibri runs on the host network (like app-kolibri) on PORT+1 (HTTP) and PORT+2
# (zip content), and imports from the builder's published port, as a teacher would.
KPORT=$((PORT+1)); ZPORT=$((PORT+2))
for p in "$PORT" "$KPORT" "$ZPORT"; do
  if (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null; then echo "REFUSED: port $p is in use (needs $PORT..$ZPORT free)" >&2; exit 2; fi
done
ARCH=$(uname -m)
[[ "$ARCH" == aarch64 || "$ARCH" == arm64 || "${ALLOW_NON_ARM64:-}" == 1 ]] || { echo "REFUSED: arch $ARCH is not arm64 (set ALLOW_NON_ARM64=1 for a dry run)" >&2; exit 2; }
cd "$(dirname "$0")/.." || exit 2
ROOT=$PWD
EV=${2:-"$ROOT/pi-evidence-$HOST-$(date -u +%Y%m%dT%H%M%SZ)"}; mkdir -p "$EV"; EV=$(cd "$EV" && pwd)
LOG=$EV/test.log; : > "$LOG"
P=cbtest$$; IMG=koenswings/kolibri-channel-builder:pitest-$P; T=$(mktemp -d)
pass=0; rc=0
say(){ echo "$*" | tee -a "$LOG"; }
ok(){ pass=$((pass+1)); say "ok: $*"; }
dc(){ (cd "$T" && port=$PORT hostname=$HOST docker compose -p "$P" -f compose.yaml -f override.yaml "$@"); }
{ echo "host=$HOST arch=$ARCH port=$PORT"; date -u; git rev-parse HEAD 2>/dev/null; docker version --format 'docker {{.Server.Version}} {{.Server.Arch}}'; docker compose version; } > "$EV/env.txt" 2>&1
docker image ls --format '{{.Repository}}:{{.Tag}}' | sort > "$EV/images-before.txt"
docker ps -a --format '{{.Names}} {{.Image}}' | sort > "$EV/containers-before.txt"
CLEANED=
cleanup(){
  [[ -n "$CLEANED" ]] && return; CLEANED=1
  dc logs --no-color > "$EV/builder.log" 2>&1 || true
  docker logs "$P-kolibri" > "$EV/kolibri.log" 2>&1 || true
  docker rm -f "$P-kolibri" >/dev/null 2>&1 || true
  dc down -v --remove-orphans >/dev/null 2>&1 || true
  docker run --rm -u 0 -v "$T:/t" --entrypoint sh "$IMG" -c 'rm -rf /t/* /t/.[!.]*' >/dev/null 2>&1 || true
  rm -rf "$T"
  if [[ "${KEEP_IMAGES:-}" != 1 ]]; then
    docker image rm "$IMG" >/dev/null 2>&1 || true
    for i in $(comm -13 "$EV/images-before.txt" <(docker image ls --format '{{.Repository}}:{{.Tag}}' | sort)); do
      [[ "$i" == *"<none>"* ]] || docker image rm "$i" >/dev/null 2>&1 || true; done
    docker image prune -f --filter "label=cbtest=$P" >/dev/null 2>&1 || true
  fi
  docker image ls --format '{{.Repository}}:{{.Tag}}' | sort > "$EV/images-after.txt"
  docker ps -a --format '{{.Names}} {{.Image}}' | sort > "$EV/containers-after.txt"
}
die(){ say "FAIL: $*"; rc=1; [[ -n "${PITEST_HOLD:-}" ]] && { say "PITEST_HOLD: holding ${PITEST_HOLD}s before cleanup"; sleep "$PITEST_HOLD"; }; cleanup; echo "rc=$rc" > "$EV/result.txt"; say "PI-TEST FAIL — evidence: $EV"; exit 1; }
trap 'die interrupted' INT TERM HUP
trap cleanup EXIT

# 1. image build (multi-arch Dockerfile, native arch)
{ docker build --label "cbtest=$P" -t "$IMG" . > "$EV/build.log" 2>&1 || { sleep 10; docker build --label "cbtest=$P" -t "$IMG" . >> "$EV/build.log" 2>&1; }; } || die "docker build (see build.log)"
ok "image built ($IMG)"

# 2. sample teacher files, made inside the image (has ffmpeg + python3; no host deps)
mkdir -p "$T/files" "$T/data/export" "$T/data/work"; chmod 777 "$T/data/export" "$T/data/work"
docker run --rm -u 0 -v "$ROOT/tests:/tests:ro" -v "$T/files:/out" --entrypoint sh "$IMG" /tests/make_samples.sh /out >> "$LOG" 2>&1 || die "make_samples"
ok "sample files: $(cd "$T/files" && find . -type f | wc -l) files"

# 3. real compose.yaml, test image tag + test files mount
cp compose.yaml "$T/"
cat > "$T/override.yaml" <<YML
services:
  builder:
    image: $IMG
    volumes:
      - ./files:/mnt/idea-files:ro
YML
dc config > "$EV/compose-config.yaml" 2>&1 || die "compose config"
dc up -d >> "$LOG" 2>&1 || die "compose up"
i=0; until curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; do i=$((i+1)); [[ $i -gt 60 ]] && die "builder /healthz not up"; sleep 1; done
ok "builder up on :$PORT"
CID=$(dc ps -q builder)
mem=$(docker inspect -f '{{.HostConfig.Memory}}' "$CID")
[[ "$mem" == 314572800 ]] || die "mem_limit is $mem, want 314572800 (300m)"
ok "mem_limit 300m enforced"

# 4. build a channel over HTTP
curl -fsS -H 'Accept: application/json' --data-urlencode source=files --data-urlencode folder=. \
  --data-urlencode "name=Pi test $P" --data-urlencode "author=Pi Tester" \
  "http://127.0.0.1:$PORT/build" > "$EV/build-result.json" 2>>"$LOG" || die "POST /build"
CH=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["channel_id"])' "$EV/build-result.json") || die "no channel_id"
NRES=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["resources"])' "$EV/build-result.json")
ok "channel $CH built ($NRES resources)"
docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' "$CID" | tee -a "$EV/memory.txt" >> "$LOG"
cat "/sys/fs/cgroup/system.slice/docker-$(docker inspect -f '{{.Id}}' "$CID").scope/memory.peak" >> "$EV/memory.txt" 2>/dev/null || true
[[ "$(docker inspect -f '{{.State.OOMKilled}} {{.State.Running}}' "$CID")" == "false true" ]] || die "builder OOM-killed or stopped"
ok "builder survived build within 300m"
curl -fsS "http://127.0.0.1:$PORT/api/public/v1/channels" > "$EV/peer-channels.json" || die "peer channel list"
grep -q "$CH" "$EV/peer-channels.json" || die "channel not listed by peer API"
ok "channel listed on peer API"

# 5. throwaway Kolibri 0.15.5, network import from the builder
docker run -d --name "$P-kolibri" --label "cbtest=$P" --network host -u 0 \
  -e KOLIBRI_HOME=/tmp/kh -e KOLIBRI_RUN_MODE=test -e KOLIBRI_HTTP_PORT="$KPORT" -e KOLIBRI_ZIP_CONTENT_PORT="$ZPORT" --entrypoint sh "$IMG" -c \
  "set -e; kolibri --version; kolibri manage migrate >/dev/null;
   kolibri manage importchannel network $CH --baseurl http://127.0.0.1:$PORT;
   kolibri manage importcontent network $CH --baseurl http://127.0.0.1:$PORT;
   touch /tmp/imported; exec kolibri start --foreground --port $KPORT" > /dev/null || die "kolibri container start"
i=0; until docker exec "$P-kolibri" test -f /tmp/imported 2>/dev/null; do
  i=$((i+1)); [[ "$(docker inspect -f '{{.State.Running}}' "$P-kolibri")" == true ]] || die "kolibri import failed (see kolibri.log)"
  [[ $i -gt 900 ]] && die "kolibri import timeout"; sleep 1; done
docker exec "$P-kolibri" kolibri --version 2>&1 | grep -q '0.15.5' || die "throwaway Kolibri is not 0.15.5"
ok "network import into throwaway Kolibri 0.15.5"
docker exec -e KOLIBRI_HOME=/tmp/kh "$P-kolibri" kolibri manage shell -c "
import json
from kolibri.core.content.models import ContentNode, File
r=ContentNode.objects.filter(channel_id='$CH').exclude(kind='topic')
h=[f.local_file_id+'.'+f.local_file.extension for f in File.objects.filter(contentnode__in=r.filter(kind='html5'))]
print('RESULT',json.dumps(dict(total=r.count(),available=r.filter(available=True).count(),kinds=sorted(r.values_list('kind',flat=True)),html5=h)))
" > "$EV/kolibri-verify.txt" 2>&1 || die "verify query"
V=$(sed -n 's/^RESULT //p' "$EV/kolibri-verify.txt")
python3 - "$V" "$NRES" <<'PY' >> "$LOG" 2>&1 || die "resource availability (see kolibri-verify.txt)"
import json,sys
v=json.loads(sys.argv[1]); n=int(sys.argv[2])
print(v)
assert v["total"]==n and v["available"]==n, (v["total"],v["available"],n)
assert "html5" in v["kinds"] and v["html5"], "no image (html5) resource"
PY
ok "all $NRES resources available in Kolibri"

# 6. image resource HTML5 page over HTTP from the throwaway Kolibri
Z=$(python3 -c 'import json,sys;print(json.loads(sys.argv[1])["html5"][0])' "$V")
i=0; got=
until [[ -n "$got" ]]; do
  for u in "http://127.0.0.1:$ZPORT/content/zipcontent/$Z/index.html" "http://127.0.0.1:$ZPORT/zipcontent/$Z/index.html" "http://127.0.0.1:$KPORT/zipcontent/$Z/index.html" "http://127.0.0.1:$KPORT/content/zipcontent/$Z/index.html"; do
    if curl -fsS "$u" -o "$EV/html5-page.html" 2>/dev/null && grep -qi "<img" "$EV/html5-page.html"; then got=$u; break; fi
  done
  i=$((i+1)); [[ $i -gt 90 ]] && die "image HTML5 page not served over HTTP"; [[ -n "$got" ]] || sleep 2
done
echo "$got" > "$EV/html5-url.txt"
SRC=$(grep -o '<img src="[^"]*"' "$EV/html5-page.html" | head -1 | sed 's/<img src="//; s/"$//')
[[ -n "$SRC" ]] || die "no <img src> in HTML5 page"
code=$(curl -s -o "$EV/html5-image.bin" -w '%{http_code}' "${got%index.html}$SRC")
[[ "$code" == 200 && -s "$EV/html5-image.bin" ]] || die "embedded image $SRC: HTTP $code"
ok "image resource HTML5 page served: $got"

cleanup
left=$(docker ps -a --format '{{.Names}}' | grep "^$P" || true)
[[ -z "$left" ]] || { say "FAIL: leftover containers: $left"; rc=1; }
docker image inspect "$IMG" >/dev/null 2>&1 && [[ "${KEEP_IMAGES:-}" != 1 ]] && { say "FAIL: test image left"; rc=1; }
echo "rc=$rc" > "$EV/result.txt"
if [[ $rc == 0 ]]; then say "PI-TEST PASS ($pass checks) — evidence: $EV"; else say "PI-TEST FAIL — evidence: $EV"; fi
exit $rc

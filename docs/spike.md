# Spike: Kolibri channel builder (Option C) — RESULT: PASS

Date: 2026-10-11, box (amd64, no Pi, no SSH). Builder code: `../app-kolibri-channel-builder/builder/channel_builder/build.py`.

## Version check
`koenswings/app-kolibri` compose.yaml uses `koenswings/kolibri:1.0-0.15.5-dev` (`./kolibri-0.15.5`), app.yaml
`current_version: "1.0-0.15.5-dev"`. **Kolibri is 0.15.5, not 0.19.5** as the design assumed. Spike pinned 0.15.5
(content schema version 5).

## Setup
```sh
cd /workspace/kolibri-studio/spike
uv venv -p 3.9 venv015 && VIRTUAL_ENV=venv015 uv pip install kolibri==0.15.5 "setuptools<70"   # 0.15 needs pkg_resources
../app-kolibri-channel-builder/tests/make_samples.sh sample   # PDF, EPUB, MP4 (h264/aac), MP4 (mpeg4), MP3, PNG, .docx, hidden + empty dir
```
Approach: the channel sqlite is created with Kolibri's **own** SQLAlchemy content schema
(`kolibri.core.content.contentschema.versions.content_schema_current`), `min_schema_version = CONTENT_SCHEMA_VERSION` ("5"),
le_utils kinds/presets/licences from Kolibri's bundled `kolibri.dist.le_utils`. Storage `KOLIBRI_DATA/content/storage/<m>/<d>/<md5>.<ext>`.

## Build (+ peak RSS)
```sh
KOLIBRI_HOME=/tmp/kh-builder PYTHONPATH=../app-kolibri-channel-builder/builder \
  python3 peak_rss.py venv015/bin/python -m channel_builder.build sample export \
  --name "Spike Channel" --author "Ms Teacher" --source-id spike/sample
```
Result: channel `84676e570c4958b3a8df4fdee25245a5` v1, 6 resources, 5 topics (empty dir dropped), `notes.docx` skipped,
warning for `Old_codec.mp4` (mpeg4). **Peak RSS 56.9 MiB** (max of builder and ffprobe children).
400 MB file: peak 37.8 MiB (streamed copy/md5). Web server (stdlib) during 3 builds: VmHWM 41 MiB.

## Import into clean Kolibri 0.15.5 (disk)
```sh
export KOLIBRI_HOME=/tmp/kh-clean CID=84676e570c4958b3a8df4fdee25245a5
venv015/bin/kolibri manage migrate
venv015/bin/kolibri manage importchannel disk $CID $PWD/export/KOLIBRI_DATA
venv015/bin/kolibri manage importcontent disk $CID $PWD/export/KOLIBRI_DATA
venv015/bin/kolibri manage shell -c "import sys; sys.argv=['v','$CID']; exec(open('verify.py').read())"
```
Gotchas found: (1) the CLI dir arg is the `KOLIBRI_DATA` folder itself in 0.15 (passing its parent fails);
(2) `last_updated` must be `YYYY-MM-DD HH:MM:SS.ffffff` (ISO `T` breaks Django's sqlite parser).
DB result: 6/6 resources `available=True` (audio, html5 image, epub, pdf, 2× video), licence
"All Rights Reserved"/"School use", author "Ms Teacher".

API (`kolibri start --port 18080`): `/api/content/channel/` lists "Spike Channel" v1 available;
`/api/content/contentnode/?channel_id=…` returns 11 nodes all available with expected extensions;
`/content/storage/…mp4` and `…pdf` return 200. Not checked: HTML5-zip rendering in a browser
(`/zipcontent/` returned 404 on the main port; 0.15 serves zipcontent on a separate origin).

Rebuild with an extra file → v2, re-import → 7/7 available (update path works, same channel id).

## Import via network (teacher-click path)
The builder's server implements Kolibri's read-only peer API (`/api/public/info/`, `/api/public/v1/channels[/lookup/<id>]`,
`POST /api/public/v1/file_checksums/<id>`, `/content/databases|storage/...`).
```sh
KOLIBRI_HOME=/tmp/kh-builder PYTHONPATH=… FILES_ROOT=$PWD/files EXPORT_DIR=$PWD/srvexport PORT=18090 venv015/bin/python -m channel_builder.server &
curl -H "Accept: application/json" --data "source=files&folder=teacher-disk/Science&name=Net Channel&author=Ms T" http://127.0.0.1:18090/build
KOLIBRI_HOME=/tmp/kh-net kolibri manage importchannel network <id> --baseurl http://127.0.0.1:18090   # + importcontent network
```
Result: 5/5 available. Kolibri's own `NetworkClient(address='127.0.0.1:18090')` accepts it as a Kolibri peer, so
"Device → Channels → Import → Local network → add address" works without touching app-kolibri.

## Verdict
PASS: a channel built offline with Kolibri 0.15.5's own schema imports cleanly (disk and network) into a clean
Kolibri 0.15.5; all resource types show up available; peak RSS ≈ 57 MiB (budget 300 MB).
Automated: 20 tests in the app repo (incl. disk + network import into a clean Kolibri) pass in ~21 s.
Not done: arm64 run, Docker image build (no docker on box; CI builds amd64+arm64), browser UI check of each renderer.

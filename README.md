# app-kolibri-channel-builder

IDEA App Disk: teachers turn their own folders into a **Kolibri channel**, fully offline, and
import it into the local Kolibri (app-kolibri). Replaces a full Kolibri Studio port
(design: Option C, approved).

- **Folder = topic, file = resource.** v1 types: PDF, EPUB (document), MP4/WebM (video, kept as-is,
  warning for codecs other than H.264/VP8/VP9 + AAC/MP3/Opus/Vorbis), MP3 (audio), images
  (PNG/JPG/GIF/WebP/SVG, wrapped in a tiny HTML5 zip because Kolibri 0.15 has no image kind).
  Other files are skipped and listed. Hidden files/folders and empty folders are ignored.
- **Sources:** a Files Disk folder (Engine `filesMount` at `/mnt/idea-files`, only read), or a
  Nextcloud folder via WebDAV (`NEXTCLOUD_URL`). When `NEXTCLOUD_URL` is set, every build needs
  a Nextcloud login; any teacher account can build. Credentials are used for that request only.
- **Metadata:** licence defaults to *All Rights Reserved* / "School use" (editable);
  author = teacher name (defaults to the Nextcloud display name).
- **Stable channel id** from the source (Files Disk path or Nextcloud user+path): rebuilding
  the same folder updates the channel (version +1) instead of creating a duplicate.
- **Import only on click.** The app serves the read-only Kolibri peer endpoints
  (`/api/public/info`, `/api/public/v1/channels`, `/content/...`). In Kolibri:
  *Device → Channels → Import → Local network or internet → add `http://<pi>:<port>`* → pick
  the channel → Import. Nothing is imported automatically.
  CLI equivalent: `kolibri manage importchannel network <id> --baseurl http://<pi>:<port>` then
  `importcontent network ...`; or from the export folder:
  `importchannel disk <id> <export>/KOLIBRI_DATA` + `importcontent disk ...`.
- **Footprint:** one Python container, no DB/broker. Measured peak RSS ~40–57 MB
  (builder incl. ffprobe, server during builds); `mem_limit: 300m`. Files are streamed,
  so RAM does not grow with file size (400 MB file: 38 MB peak).

## Kolibri version coupling

The channel DB is written with Kolibri's own SQLAlchemy content schema
(`kolibri.core.content.contentschema.versions.content_schema_current`, schema 5), so the
builder pins the **same Kolibri version as app-kolibri: 0.15.5**. Bump both together; CI's
import test catches schema drift.

## Layout

| Path | What |
|---|---|
| `compose.yaml` | IDEA app (x-app, filesMount, export volume, `mem_limit: 300m`) |
| `app.yaml` | build/version metadata |
| `Dockerfile` | multi-arch image (python:3.9-slim + kolibri 0.15.5 + ffprobe) |
| `builder/channel_builder/build.py` | folder → channel export (CLI: `python -m channel_builder.build SRC OUT --name N --author A`) |
| `builder/channel_builder/server.py` | teacher web UI + Kolibri peer endpoints |
| `builder/channel_builder/webdav.py` | Nextcloud WebDAV (stdlib) |
| `ci/github-workflow-ci.yml` | GitHub Actions (tests + amd64/arm64 image build); move to `.github/workflows/` to enable |
| `tests/` | unit + integration tests (import into clean Kolibri 0.15.5, disk and network) |

## Tests

```sh
python3.9 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest   # needs ffmpeg
PATH=.venv/bin:$PATH python -m pytest -v tests
```

See [docs/INDEX.md](docs/INDEX.md).

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

## USB sticks and automatic builds (idea#193)

- The Engine mounts FAT/exFAT sticks read-only and binds them in at `/mnt/idea-usb`
  (`x-app.usbMount`), with `/mnt/idea-usb/.idea-usb.json` listing them
  (`stickId: "usb-<fs uuid>"`, `device`, `label`, `displayName`). Only sticks in that
  JSON are used. The builder never writes to a stick.
- Manual: pick **USB stick** as the source. The channel id comes from
  `usb:<fs uuid>:<path>`, so the same stick folder always updates the same channel,
  in any USB port.
- Automatic: a stick with a top-level `kolibri` folder (any case) gets one channel
  per subfolder (`kolibri/Grade 5 Science` → "Grade 5 Science"). An optional
  `channel.json` in that folder sets `name`, `author`, `description`, `license`
  (a Kolibri licence id such as `CC BY`) and `license_description`. Bad JSON gives a
  warning and the defaults (folder name, stick label as author, All Rights Reserved).
- One FIFO queue runs all builds, manual and automatic, one at a time. A stick that
  hasn't changed (same paths, sizes and mtimes) is skipped. Sticks over
  `AUTO_BUILD_MAX_GB` (8) are refused for auto-build. If a stick is pulled out
  mid-build, that build fails ("plug it in again"), the previous version stays
  served, and the stick is rebuilt when it goes back in.
- Status: the "From USB sticks" box on the start page, and `GET /api/usb-status`.
  Also `GET /api/queue` and `GET /api/jobs/<id>`. The page polls them.
- `AUTO_BUILD=0` switches automatic builds off.

## Building the image (no published image yet)

`compose.yaml` uses `pull_policy: never`, so the image has to exist on the Pi:

```sh
# on the Pi (arm64). On some Pis Docker builds get no DNS: --network host fixes that
docker build --network host -t koenswings/kolibri-channel-builder:1.0-0.15.5 .
```

`docs/publish-image.yml` is a ready GitHub Actions job that would publish a
multi-arch image to `ghcr.io/koenswings/kolibri-channel-builder`. Someone with the
`workflow` scope has to copy it to `.github/workflows/`. After the first publish,
compose can point at the ghcr image with `pull_policy: missing`.

## Kolibri address

`.local` names don't resolve inside the Kolibri container. The page shows an IP to
add in Kolibri: `LAN_IP` if set, otherwise the IP the browser used. If neither is
available, it asks for the Pi's IP address from the Console.

The port in that address is the **published host port**, not the container port
8090. compose sets `PUBLIC_PORT=${port}` (the port the Engine assigns, e.g. 18400 on
idea04). Without `PUBLIC_PORT` the page uses the port the browser used, and only
falls back to 8090 if neither is known.

## Data folders and permissions

Docker (or the Engine) creates `./data/export` and `./data/work` as root when they
don't exist yet. The image's entrypoint therefore starts as root, creates both
folders, chowns them to the `builder` user (uid/gid 1000, override with
`BUILDER_UID`/`BUILDER_GID`) only when needed, and then drops privileges with
`setpriv`. The server itself never runs as root.

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

## Picker and look (house style)

- The source and folder pickers always name each Files Disk (`Files Disk: <shareName>` from the Engine's
  `.idea-files.json`, falling back to the disk folder) and each USB stick (`displayName` from `.idea-usb.json`),
  even when only one is present; folders are grouped per disk/stick (`<optgroup>`). Repeated names get ` (2)`,
  ` (3)`. The submitted value stays the stable disk folder / `stickId` path, never the display name.
- `.idea-usb.json` (Engine PR #205, `docs/USB-STICKS.md`) is the complete list: a stick missing from it is gone
  even if its folder remains; unknown fields are ignored.
- The Nextcloud folder/user/password fields show only for the Nextcloud source (inline JS); without JS all fields
  are shown with a note. Styling follows the IDEA house style (red/blue headings, callout cards, blue table
  headers, DejaVu Sans/system fonts), fully offline, responsive down to phone width.

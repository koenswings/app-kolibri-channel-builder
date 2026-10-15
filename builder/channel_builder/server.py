"""Teacher-facing web UI + read-only Kolibri "peer" endpoints for built channels.

Kolibri (Device > Channels > Import > Local network) can add this server's
address and import the channels from it; nothing is imported until a teacher
or admin clicks Import there. The same export folder also works for
`kolibri manage importchannel disk <id> <EXPORT_DIR>/KOLIBRI_DATA`.
"""
import gzip
import html
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from channel_builder.build import Builder, DEFAULT_LICENSE, DEFAULT_LICENSE_DESCRIPTION
from channel_builder import webdav
from le_utils.constants import licenses

log = logging.getLogger("channel_builder.server")

FILES_ROOT = os.environ.get("FILES_ROOT", "/mnt/idea-files")
EXPORT_DIR = os.environ.get("EXPORT_DIR", "/data/export")
WORK_DIR = os.environ.get("WORK_DIR", "/data/work")
NEXTCLOUD_URL = os.environ.get("NEXTCLOUD_URL", "").strip()
NC_VERIFY_TLS = os.environ.get("NEXTCLOUD_VERIFY_TLS", "1") != "0"
REQUIRE_LOGIN = os.environ.get("REQUIRE_NEXTCLOUD_LOGIN", "1" if NEXTCLOUD_URL else "0") == "1"
DEVICE_NAME = os.environ.get("DEVICE_NAME") or "IDEA channel builder (%s)" % socket.gethostname()
INSTANCE_ID = os.environ.get("INSTANCE_ID", "1dea0c4a1b1100000000000000000000")
KOLIBRI_VERSION = os.environ.get("KOLIBRI_VERSION", "0.15.5")

BUILD_LOCK = threading.Lock()
HEX32 = re.compile(r"^[0-9a-f]{32}$")
STORAGE = re.compile(r"^/content/storage/([0-9a-f])/([0-9a-f])/([0-9a-f]{32}\.[a-z0-9]{1,10})$")
DBFILE = re.compile(r"^/content/databases/([0-9a-f]{32})\.sqlite3$")
CTYPES = {"mp4": "video/mp4", "webm": "video/webm", "mp3": "audio/mpeg", "pdf": "application/pdf",
          "epub": "application/epub+zip", "zip": "application/zip", "sqlite3": "application/octet-stream"}


def kdata():
    return os.path.join(EXPORT_DIR, "KOLIBRI_DATA", "content")


def list_channels():
    out = []
    d = os.path.join(kdata(), "databases")
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".sqlite3") or not HEX32.match(fn[:-8]):
            continue
        try:
            c = sqlite3.connect("file:%s?mode=ro" % os.path.join(d, fn), uri=True)
            c.row_factory = sqlite3.Row
            r = c.execute("select id,name,description,author,version,last_updated,published_size,"
                          "total_resource_count,tagline from content_channelmetadata").fetchone()
            c.close()
            if r:
                out.append(dict(r))
        except sqlite3.Error as e:
            log.warning("bad channel db %s: %s", fn, e)
    return out


def public_repr(ch):
    """Shape of Kolibri 0.15 /api/public/v1/channels entries."""
    return {"id": ch["id"], "name": ch["name"], "language": None, "description": ch["description"],
            "tagline": ch["tagline"], "total_resource_count": ch["total_resource_count"],
            "version": ch["version"], "published_size": ch["published_size"],
            "last_published": ch["last_updated"], "icon_encoding": "", "matching_tokens": [],
            "public": False, "included_languages": []}


def files_disk_folders(max_depth=3):
    """Folders a teacher can pick on mounted Files Disks (relative to FILES_ROOT)."""
    res = []
    if not os.path.isdir(FILES_ROOT):
        return res
    root_depth = FILES_ROOT.rstrip("/").count("/")
    for cur, dirs, _ in os.walk(FILES_ROOT):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        depth = cur.rstrip("/").count("/") - root_depth
        if depth >= max_depth:
            dirs[:] = []
        if depth >= 1:
            res.append(os.path.relpath(cur, FILES_ROOT))
    return res


def safe_files_path(rel):
    p = os.path.realpath(os.path.join(FILES_ROOT, rel.lstrip("/")))
    root = os.path.realpath(FILES_ROOT)
    if not (p == root or p.startswith(root + os.sep)) or not os.path.isdir(p):
        raise ValueError("folder not found on Files Disk")
    return p


def build_from_form(f):
    """f: dict of form fields. Returns result dict. Raises ValueError/AuthError."""
    name = f.get("name", "").strip()
    if not name:
        raise ValueError("channel name is required")
    author = f.get("author", "").strip()
    nc = None
    if REQUIRE_LOGIN or f.get("source") == "nextcloud":
        if not NEXTCLOUD_URL:
            raise ValueError("Nextcloud is not configured")
        nc = webdav.Nextcloud(NEXTCLOUD_URL, f.get("nc_user", "").strip(), f.get("nc_password", ""), NC_VERIFY_TLS)
        display = nc.display_name()  # raises AuthError on bad login
        author = author or display
    if not author:
        raise ValueError("author (teacher name) is required")
    lic = f.get("license") or DEFAULT_LICENSE
    if lic not in dict(licenses.choices):
        raise ValueError("unknown licence")
    lic_desc = f.get("license_description", DEFAULT_LICENSE_DESCRIPTION)
    with BUILD_LOCK:
        if f.get("source") == "nextcloud":
            folder = f.get("nc_folder", "").strip()
            os.makedirs(WORK_DIR, exist_ok=True)
            tmp = tempfile.mkdtemp(dir=WORK_DIR)
            try:
                nc.download(folder, tmp)
                b = Builder(tmp, EXPORT_DIR, name, author, "nc:%s:%s" % (nc.user, folder.strip("/")),
                            f.get("description", ""), lic, lic_desc)
                return b.build()
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        rel = f.get("folder", "")
        src = safe_files_path(rel)
        b = Builder(src, EXPORT_DIR, name, author, "files:%s" % os.path.relpath(src, os.path.realpath(FILES_ROOT)),
                    f.get("description", ""), lic, lic_desc)
        return b.build()


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Kolibri channel builder</title><style>body{{font-family:sans-serif;max-width:46em;margin:2em auto;padding:0 1em}}
label{{display:block;margin:.6em 0 .2em}}input,select,textarea{{width:100%;padding:.4em}}button{{margin-top:1em;padding:.6em 1.2em}}
.warn{{color:#a60}}.err{{color:#b00}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:.3em;text-align:left}}</style></head>
<body><h1>Make a Kolibri channel</h1>{msg}
<p>Each folder becomes a topic, each file a resource. Supported: PDF, EPUB, MP4/WebM (H.264/VP8/VP9), MP3, images.</p>
<form method="post" action="/build">
<label>Where are the files?</label><select name="source">{sources}</select>
<label>Files Disk folder</label><select name="folder">{folders}</select>
<label>Nextcloud folder (if source is Nextcloud), e.g. Documents/Science</label><input name="nc_folder">
{login}
<label>Channel name</label><input name="name" required>
<label>Description</label><textarea name="description" rows="2"></textarea>
<label>Author (teacher name; empty = your Nextcloud name)</label><input name="author">
<label>Licence</label><select name="license">{licenses}</select>
<label>Licence description</label><input name="license_description" value="{licdesc}">
<button type="submit">Build channel</button></form>
<h2>Built channels</h2><table><tr><th>Name</th><th>Version</th><th>Resources</th><th>Author</th></tr>{channels}</table>
<p><b>To import into Kolibri</b> (nothing is imported automatically): in Kolibri open
Device &rarr; Channels &rarr; Import &rarr; Local network or internet, add the address
<code>http://&lt;this-pi&gt;:{port}</code>, pick the channel and click Import.</p></body></html>"""


def render(msg="", port=8090):
    opts = lambda items, sel=None: "".join(
        '<option value="%s"%s>%s</option>' % (html.escape(v), " selected" if v == sel else "", html.escape(t))
        for v, t in items)
    sources = [("files", "Files Disk")] + ([("nextcloud", "Nextcloud")] if NEXTCLOUD_URL else [])
    login = ('<label>Nextcloud user</label><input name="nc_user" autocomplete="username">'
             '<label>Nextcloud password or app password</label><input type="password" name="nc_password">'
             if NEXTCLOUD_URL else "")
    chans = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % tuple(
        html.escape(str(c[k])) for k in ("name", "version", "total_resource_count", "author"))
        for c in list_channels())
    return PAGE.format(msg=msg, sources=opts(sources),
                       folders=opts([(f, f) for f in files_disk_folders()]),
                       login=login, licenses=opts(licenses.choices, DEFAULT_LICENSE),
                       licdesc=html.escape(DEFAULT_LICENSE_DESCRIPTION), channels=chans, port=port)


class Handler(BaseHTTPRequestHandler):
    server_version = "idea-channel-builder"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def _file(self, path):
        if not os.path.isfile(path):
            return self._json({"error": "not found"}, 404)
        ext = path.rsplit(".", 1)[-1]
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", CTYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(size))
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile, 1 << 20)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        if p in ("/", "/index.html"):
            return self._send(200, render(port=self.server.server_address[1]), "text/html; charset=utf-8")
        if p == "/healthz":
            return self._json({"ok": True})
        if p.rstrip("/") == "/api/public/info":
            return self._json({"application": "kolibri", "kolibri_version": KOLIBRI_VERSION,
                               "instance_id": INSTANCE_ID, "device_name": DEVICE_NAME,
                               "operating_system": "Linux", "subset_of_users_device": False})
        m = re.match(r"^/api/public/v1/channels(?:/lookup/([0-9a-f]{32}))?/?$", p)
        if m:
            chans = list_channels()
            if m.group(1):
                chans = [c for c in chans if c["id"] == m.group(1)]
                if not chans:
                    return self._json({"id": "NOT_FOUND"}, 404)
            kw = urllib.parse.parse_qs(u.query).get("keyword", [""])[0].lower()
            if kw:
                chans = [c for c in chans if kw in c["name"].lower() or kw in (c["description"] or "").lower()]
            return self._json([public_repr(c) for c in chans])
        m = DBFILE.match(p)
        if m:
            return self._file(os.path.join(kdata(), "databases", m.group(1) + ".sqlite3"))
        m = STORAGE.match(p)
        if m and m.group(3)[0] == m.group(1) and m.group(3)[1] == m.group(2):
            return self._file(os.path.join(kdata(), "storage", m.group(1), m.group(2), m.group(3)))
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1 << 20:
            return self._json({"error": "too large"}, 413)
        body = self.rfile.read(n)
        m = re.match(r"^/api/public/v1/file_checksums/([0-9a-f]{32})/?$", p)
        if m:
            if self.headers.get("Content-Type", "").startswith("application/gzip"):
                body = gzip.decompress(body)
            checksums = json.loads(body.decode())
            sdir = os.path.join(kdata(), "storage")
            have = set()
            for root, _, files in os.walk(sdir):
                have.update(f.split(".")[0] for f in files if not f.endswith(".tmp"))
            mask = sum(int(c in have) << i for i, c in enumerate(checksums))
            return self._send(200, str(mask), "application/octet-stream")
        if p == "/build":
            form = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode(), keep_blank_values=True).items()}
            try:
                r = build_from_form(form)
            except webdav.AuthError as e:
                return self._send(403, render('<p class="err">%s</p>' % html.escape(str(e))), "text/html; charset=utf-8")
            except (ValueError, SystemExit, OSError) as e:
                return self._send(400, render('<p class="err">%s</p>' % html.escape(str(e))), "text/html; charset=utf-8")
            msg = '<p>Built <b>%s</b> version %d: %d resources.</p>' % (
                html.escape(form.get("name", "")), r["version"], r["resources"])
            msg += "".join('<p class="warn">%s</p>' % html.escape(w) for w in r["warnings"])
            if r["skipped"]:
                msg += '<p class="warn">Skipped unsupported files: %s</p>' % html.escape(", ".join(r["skipped"]))
            if self.headers.get("Accept", "").startswith("application/json"):
                return self._json(r)
            return self._send(200, render(msg, self.server.server_address[1]), "text/html; charset=utf-8")
        return self._json({"error": "not found"}, 404)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    port = int(os.environ.get("PORT", "8090"))
    os.makedirs(EXPORT_DIR, exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("channel builder on :%d, files=%s export=%s nextcloud=%s", port, FILES_ROOT, EXPORT_DIR, NEXTCLOUD_URL or "-")
    srv.serve_forever()


if __name__ == "__main__":
    main()

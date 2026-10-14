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
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from channel_builder.build import Builder, DEFAULT_LICENSE, DEFAULT_LICENSE_DESCRIPTION
from channel_builder import webdav, usb
from channel_builder.jobs import BuildQueue, Job, QUEUED, RUNNING, DONE, FAILED, SKIPPED
from le_utils.constants import licenses

log = logging.getLogger("channel_builder.server")

FILES_ROOT = os.environ.get("FILES_ROOT", "/mnt/idea-files")
EXPORT_DIR = os.environ.get("EXPORT_DIR", "/data/export")
WORK_DIR = os.environ.get("WORK_DIR", "/data/work")
NEXTCLOUD_URL = os.environ.get("NEXTCLOUD_URL", "").strip()
NC_VERIFY_TLS = os.environ.get("NEXTCLOUD_VERIFY_TLS", "1") != "0"
# Login is needed only for the Nextcloud source (UI issue 4). Set
# REQUIRE_NEXTCLOUD_LOGIN=1 to demand it for every source.
REQUIRE_LOGIN = os.environ.get("REQUIRE_NEXTCLOUD_LOGIN", "0") == "1"
USB_ROOT = os.environ.get("USB_ROOT", "/mnt/idea-usb")
AUTO_BUILD = os.environ.get("AUTO_BUILD", "1") != "0"
AUTO_BUILD_MAX_BYTES = int(float(os.environ.get("AUTO_BUILD_MAX_GB", "8")) * 1024 ** 3)
# The Pi's LAN IP for the "add this address in Kolibri" hint: .local names
# don't resolve inside the Kolibri container (found on wh). Optional.
LAN_IP = os.environ.get("LAN_IP", "").strip()
USB_POLL_SECONDS = float(os.environ.get("USB_POLL_SECONDS", "5"))
DEVICE_NAME = os.environ.get("DEVICE_NAME") or "IDEA channel builder (%s)" % socket.gethostname()
INSTANCE_ID = os.environ.get("INSTANCE_ID", "1dea0c4a1b1100000000000000000000")
KOLIBRI_VERSION = os.environ.get("KOLIBRI_VERSION", "0.15.5")

BUILD_LOCK = threading.Lock()
QUEUE = BuildQueue(BUILD_LOCK)
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


def _usb_build(stick, src, rel, name, author, description, lic, lic_desc, job):
    check = usb.make_check(USB_ROOT, stick)
    b = Builder(src, EXPORT_DIR, name, author, usb.source_id(stick, rel), description, lic, lic_desc,
                progress=job.progress, check=check)
    try:
        return b.build()
    except OSError:
        check()  # a read error on a pulled stick -> StickRemoved with a clear message
        raise


def prepare_build(f):
    """Validate the form synchronously; return (title, run(job)). Raises ValueError/AuthError."""
    name = f.get("name", "").strip()
    if not name:
        raise ValueError("Channel name is required.")
    source = f.get("source") or "files"
    author = f.get("author", "").strip()
    nc = None
    if source == "nextcloud" or REQUIRE_LOGIN:
        if not NEXTCLOUD_URL:
            raise ValueError("Nextcloud is not configured on this builder.")
        nc = webdav.Nextcloud(NEXTCLOUD_URL, f.get("nc_user", "").strip(), f.get("nc_password", ""), NC_VERIFY_TLS)
        display = nc.display_name()  # raises AuthError on a bad login
        author = author or display
    if not author:
        raise ValueError("Author (teacher name) is required.")
    lic = f.get("license") or DEFAULT_LICENSE
    if lic not in dict(licenses.choices):
        raise ValueError("Unknown licence.")
    lic_desc = f.get("license_description", DEFAULT_LICENSE_DESCRIPTION)
    desc = f.get("description", "")
    if source == "nextcloud":
        folder = f.get("nc_folder", "").strip()
        if not folder:
            raise ValueError("Nextcloud folder is required.")

        def run(job):
            os.makedirs(WORK_DIR, exist_ok=True)
            tmp = tempfile.mkdtemp(dir=WORK_DIR)
            try:
                job.message = "Downloading from Nextcloud..."
                nc.download(folder, tmp)
                job.message = ""
                return Builder(tmp, EXPORT_DIR, name, author, "nc:%s:%s" % (nc.user, folder.strip("/")),
                               desc, lic, lic_desc, progress=job.progress).build()
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        return "%s (Nextcloud: %s)" % (name, folder), run
    if source == "usb":
        sid, _, rel = f.get("usb_folder", "").partition("/")
        stick = usb.find_stick(USB_ROOT, sid)
        if not stick:
            raise ValueError("That USB stick is not plugged in (any more).")
        src = usb.safe_stick_path(stick, rel)
        rel = os.path.relpath(src, os.path.realpath(stick["path"]))
        return ("%s (%s / %s)" % (name, stick["displayName"], rel),
                lambda job: _usb_build(stick, src, rel, name, author, desc, lic, lic_desc, job))
    if source != "files":
        raise ValueError("Unknown source.")
    src = safe_files_path(f.get("folder", ""))
    rel = os.path.relpath(src, os.path.realpath(FILES_ROOT))
    return ("%s (Files Disk: %s)" % (name, rel),
            lambda job: Builder(src, EXPORT_DIR, name, author, "files:%s" % rel, desc, lic, lic_desc,
                                progress=job.progress).build())


def submit_build(f):
    title, run = prepare_build(f)
    return QUEUE.submit(Job("manual", title, run, form=f))


def build_from_form(f):
    """Synchronous build (CLI/tests): queue it and wait for the result."""
    job = submit_build(f)
    QUEUE.wait(job)
    if job.state != DONE:
        raise ValueError(job.message)
    return job.result


class AutoBuilder(object):
    """Polls USB_ROOT/.idea-usb.json; queues one build per kolibri/<channel> folder."""

    def __init__(self):
        self.present = {}   # stickId -> stick
        self.status = {}    # stickId -> dict
        self.lock = threading.Lock()
        self.state = None

    def _state(self):
        if self.state is None:
            self.state = usb.State(os.path.join(WORK_DIR, "usb-state.json"))
        return self.state

    def poll(self):
        sticks = {s["stickId"]: s for s in usb.list_sticks(USB_ROOT)}
        with self.lock:
            for sid in list(self.present):
                if sid not in sticks or sticks[sid]["device"] != self.present[sid]["device"]:
                    del self.present[sid]
                    st = self.status.get(sid)
                    if st:
                        st["present"] = False
            new = [s for sid, s in sticks.items() if sid not in self.present]
            for s in new:
                self.present[s["stickId"]] = s
        for s in new:
            self.scan(s)

    def scan(self, stick):
        st = dict(stickId=stick["stickId"], displayName=stick["displayName"], present=True,
                  state="", message="", channels=[])
        with self.lock:
            self.status[stick["stickId"]] = st
        if not AUTO_BUILD:
            st.update(state="off", message="Automatic builds are switched off on this builder.")
            return
        folders = usb.channel_folders(stick)
        if not folders:
            st.update(state="none", message="No kolibri folder on this stick: use the form below to build by hand.")
            return
        prints, total = {}, 0
        try:
            for rel in folders:
                prints[rel] = usb.fingerprint(os.path.join(stick["path"], rel))
                total += prints[rel][1]
        except OSError:
            st.update(state="failed", message=usb.REMOVED_MSG)
            return
        if total > AUTO_BUILD_MAX_BYTES:
            st.update(state="too_large", message="Too large for an automatic build (%.1f GB, limit %.0f GB): "
                      "use the manual build below." % (total / 1024 ** 3, AUTO_BUILD_MAX_BYTES / 1024 ** 3))
            return
        lic_ids = dict(licenses.choices)
        for rel in folders:
            absdir = os.path.join(stick["path"], rel)
            sid = usb.source_id(stick, rel)
            fp = prints[rel][0]
            meta, warns = usb.read_channel_json(absdir, lic_ids)
            name = meta.get("name") or rel.split("/", 1)[1]
            ch = dict(folder=rel, name=name, sourceId=sid, warnings=warns, job=None, state=QUEUED, message="")
            st["channels"].append(ch)
            from channel_builder.build import uid
            dbp = os.path.join(kdata(), "databases", uid("channel", sid) + ".sqlite3")
            if self._state().get(sid) == fp and os.path.exists(dbp):
                ch.update(state=SKIPPED, message="Unchanged since the last build.")
                continue
            ch["job"] = QUEUE.submit(Job("auto", "%s (%s)" % (name, stick["displayName"]),
                                         self._runner(stick, absdir, rel, meta, fp, sid)))

    def _runner(self, stick, absdir, rel, meta, fp, sid):
        def run(job):
            r = _usb_build(stick, absdir, rel, meta.get("name") or rel.split("/", 1)[1],
                           meta.get("author") or stick["label"], meta.get("description", ""),
                           meta.get("license") or DEFAULT_LICENSE,
                           meta.get("license_description", DEFAULT_LICENSE_DESCRIPTION), job)
            self._state().put(sid, fp)
            return r
        return run

    def public(self):
        out = []
        with self.lock:
            items = list(self.status.values())
        for st in items:
            chans = []
            for ch in st["channels"]:
                j = ch["job"]
                c = dict(folder=ch["folder"], name=ch["name"], warnings=ch["warnings"],
                         state=j.state if j else ch["state"], message=j.message if j else ch["message"])
                if j:
                    c.update(done=j.done, total=j.total, job=j.id)
                    if j.result:
                        c.update(version=j.result["version"], resources=j.result["resources"],
                                 warnings=ch["warnings"] + j.result["warnings"])
                    if j.state == QUEUED:
                        c["position"] = QUEUE.position(j)
                chans.append(c)
            state = st["state"]
            if chans:
                states = {c["state"] for c in chans}
                if states & {QUEUED, RUNNING}:
                    state = "building"
                elif FAILED in states:
                    state = "failed"
                else:
                    state = "built"
            msg = st["message"]
            if state == "built":
                msg = "Built: you can pull the stick out."
            elif state == "failed" and chans:
                msg = next(c["message"] for c in chans if c["state"] == FAILED)
            out.append(dict(stickId=st["stickId"], displayName=st["displayName"], present=st["present"],
                            state=state, message=msg, channels=chans))
        return dict(autoBuild=AUTO_BUILD, maxBytes=AUTO_BUILD_MAX_BYTES, sticks=out)

    def loop(self):
        while True:
            try:
                self.poll()
            except Exception:  # pragma: no cover
                log.exception("usb poll failed")
            time.sleep(USB_POLL_SECONDS)


AUTO = AutoBuilder()


def usb_status():
    return AUTO.public()


def queue_status():
    return QUEUE.snapshot()


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Kolibri channel builder</title><style>body{{font-family:sans-serif;max-width:46em;margin:2em auto;padding:0 1em}}
label{{display:block;margin:.6em 0 .2em}}input,select,textarea{{width:100%;padding:.4em;box-sizing:border-box}}button{{margin-top:1em;padding:.6em 1.2em}}
.warn{{color:#a60}}.err{{color:#b00}}.ok{{color:#060}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:.3em;text-align:left}}
.box{{border:1px solid #ccc;border-radius:6px;padding:.6em 1em;margin:1em 0;background:#fafafa}}.box h2{{margin:.2em 0 .4em;font-size:1.1em}}
progress{{width:100%}}[hidden]{{display:none!important}}</style></head>
<body><h1>Make a Kolibri channel</h1>
<div id="job">{job}</div>
<div class="box" id="queue"><h2>Builds</h2><div id="queue-body">{queue}</div></div>
<div class="box" id="usb"><h2>From USB sticks</h2><div id="usb-body">{usb}</div></div>
<div id="msg">{msg}</div>{empty}
<p>Each folder becomes a topic, each file a resource. Supported: PDF, EPUB, MP4/WebM (H.264/VP8/VP9), MP3, images.</p>
<form method="post" action="/build" id="form">
<label for="source">Where are the files?</label><select name="source" id="source">{sources}</select>
<div data-src="files"{hid_files}><label for="folder">Files Disk folder</label><select name="folder" id="folder">{folders}</select></div>
<div data-src="usb"{hid_usb}><label for="usb_folder">USB stick folder</label><select name="usb_folder" id="usb_folder">{usb_folders}</select></div>
<div data-src="nextcloud"{hid_nc}><label for="nc_folder">Nextcloud folder, e.g. Documents/Science</label><input name="nc_folder" id="nc_folder" value="{v_nc_folder}">
<label>Nextcloud user</label><input name="nc_user" autocomplete="username" value="{v_nc_user}">
<label>Nextcloud password or app password</label><input type="password" name="nc_password"></div>
<label for="name">Channel name</label><input name="name" id="name" required value="{v_name}">
<label>Description</label><textarea name="description" rows="2">{v_description}</textarea>
<label>Author (teacher name{author_hint})</label><input name="author" id="author" value="{v_author}">
<label>Licence</label><select name="license">{licenses}</select>
<label>Licence description</label><input name="license_description" value="{v_license_description}">
<button type="submit" id="build">Build channel</button></form>
<h2>Built channels</h2><table id="channels"><tr><th>Name</th><th>Version</th><th>Resources</th><th>Author</th></tr>{channels}</table>
<p><b>To import into Kolibri</b> (nothing is imported automatically): in Kolibri open
Device &rarr; Channels &rarr; Import &rarr; Local network or internet, add the address
<code id="kolibri-address">{address}</code>, pick the channel and click Import.{address_hint}</p>
<script>
(function(){{
var src=document.getElementById('source');
function show(){{document.querySelectorAll('[data-src]').forEach(function(d){{d.hidden=d.getAttribute('data-src')!==src.value;}});}}
src.addEventListener('change',show);show();
var jobId={job_id};
function esc(s){{return String(s).replace(/[&<>"]/g,function(c){{return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c];}});}}
function tick(){{
 fetch('/api/fragments'+(jobId?'?job='+jobId:'')).then(function(r){{return r.json();}}).then(function(d){{
  document.getElementById('queue-body').innerHTML=d.queue;document.getElementById('usb-body').innerHTML=d.usb;
  document.getElementById('job').innerHTML=d.job;
  if(d.channels!==undefined)document.getElementById('channels').innerHTML=d.channels;
 }}).catch(function(){{}});
}}
setInterval(tick,{poll_ms});
}})();
</script></body></html>"""


def _state_label(j):
    if j["state"] == RUNNING:
        if j["total"]:
            return 'Building %d/%d files <progress max="%d" value="%d"></progress>' % (
                j["done"], j["total"], j["total"], j["done"])
        return "Building... " + html.escape(j.get("message") or "")
    if j["state"] == QUEUED:
        return "Queued (number %s in line): waits for the build before it." % j.get("position", "?")
    if j["state"] == DONE:
        r = j.get("result") or {}
        return '<span class="ok">Built version %s: %s resources.</span>' % (r.get("version"), r.get("resources"))
    if j["state"] == FAILED:
        return '<span class="err">Failed: %s</span>' % html.escape(j["message"])
    return html.escape(j["state"])


def render_queue():
    q = queue_status()
    rows = q["running"] + q["queued"]
    if not rows:
        return "<p>No build running. A new build starts right away.</p>"
    return "<ul>%s</ul>" % "".join("<li><b>%s</b>: %s</li>" % (html.escape(j["title"]), _state_label(j)) for j in rows)


def render_usb():
    s = usb_status()
    if not s["sticks"]:
        return ("<p>No USB stick with files for this builder. Put a folder named <code>kolibri</code> on a stick "
                "with one folder per channel inside it, and plug it in: channels are built automatically.</p>")
    out = []
    for st in s["sticks"]:
        state = st["state"] if st["present"] or st["state"] == "built" else (
            "removed" if st["state"] != "failed" else "failed")
        cls = {"failed": "err", "too_large": "warn", "built": "ok"}.get(state, "")
        msg = st["message"]
        if not st["present"]:
            msg = (msg + " " if state in ("built", "failed") else "") + "(stick pulled out)"
        items = []
        for c in st["channels"]:
            j = dict(state=c["state"], done=c.get("done", 0), total=c.get("total", 0), message=c["message"],
                     position=c.get("position"), result=dict(version=c.get("version"), resources=c.get("resources")))
            lab = "Unchanged since the last build: skipped." if c["state"] == SKIPPED else _state_label(j)
            w = "".join('<br><span class="warn">%s</span>' % html.escape(x) for x in c["warnings"])
            items.append("<li>%s: %s%s</li>" % (html.escape(c["name"]), lab, w))
        out.append('<p><b>%s</b> <span class="%s">%s</span></p><ul>%s</ul>' % (
            html.escape(st["displayName"]), cls, html.escape(msg), "".join(items)))
    return "".join(out)


def render_job(job):
    if not job:
        return ""
    j = job.public(QUEUE.position(job))
    if j["state"] == DONE:
        r = job.result
        m = '<p class="ok">Built <b>%s</b> version %d: %d resources.</p>' % (
            html.escape(job.form.get("name", job.title)), r["version"], r["resources"])
        m += "".join('<p class="warn">%s</p>' % html.escape(w) for w in r["warnings"])
        if r["skipped"]:
            m += '<p class="warn">Skipped unsupported files: %s</p>' % html.escape(", ".join(r["skipped"]))
        return '<div class="box" id="job-box">%s</div>' % m
    return '<div class="box" id="job-box"><h2>Your build: %s</h2><p>%s</p></div>' % (
        html.escape(job.title), _state_label(j))


def render_channels():
    return "<tr><th>Name</th><th>Version</th><th>Resources</th><th>Author</th></tr>" + "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % tuple(
            html.escape(str(c[k])) for k in ("name", "version", "total_resource_count", "author"))
        for c in list_channels())


IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def kolibri_address(port, host_header=""):
    """(address, hint) to type in Kolibri. An IP, never a .local name."""
    host = (host_header or "").rsplit(":", 1)[0].strip("[]")
    if LAN_IP:
        return "http://%s:%d" % (LAN_IP, port), ""
    if IPV4.match(host) and not host.startswith("127."):
        return "http://%s:%d" % (host, port), ""
    return ("http://&lt;IP address of this Pi&gt;:%d" % port,
            " Use the Pi's IP address (shown in the Console), not a <code>.local</code> name:"
            " Kolibri cannot look up .local names.")


def render(msg="", port=8090, values=None, job=None, host=""):
    v = dict(values or {})
    opts = lambda items, sel=None: "".join(
        '<option value="%s"%s>%s</option>' % (html.escape(a), " selected" if a == sel else "", html.escape(t))
        for a, t in items)
    sticks = usb.list_sticks(USB_ROOT)
    sources = [("files", "Files Disk")] + ([("usb", "USB stick")] if sticks or v.get("source") == "usb" else []) + \
        ([("nextcloud", "Nextcloud")] if NEXTCLOUD_URL else [])
    src = v.get("source") if v.get("source") in dict(sources) else "files"
    usb_folders = [("%s/%s" % (s["stickId"], f), "%s / %s" % (s["displayName"], f))
                   for s in sticks for f in usb.stick_folders(s)]
    e = lambda k, d="": html.escape(v.get(k, d))
    hid = lambda name: "" if name == src else " hidden"
    files_folders = files_disk_folders()
    empty = ""
    if not files_folders and not usb_folders and not NEXTCLOUD_URL:
        empty = ('<div class="box" id="empty"><h2>No folders to build from yet</h2><p>This builder finds no '
                 'Files Disk folders, no USB stick and no Nextcloud. To get started, plug in a USB stick with '
                 'your files (a <code>kolibri</code> folder on it is built automatically), or ask your IDEA '
                 'operator to dock a Files Disk or to connect Nextcloud.</p></div>')
    address, address_hint = kolibri_address(port, host)
    return PAGE.format(
        msg=msg, job=render_job(job), job_id=json.dumps(job.id if job else None),
        queue=render_queue(), usb=render_usb(), sources=opts(sources, src),
        folders=opts([(f, f) for f in files_folders], v.get("folder")), empty=empty,
        address=address, address_hint=address_hint,
        usb_folders=opts(usb_folders, v.get("usb_folder")),
        hid_files=hid("files"), hid_usb=hid("usb"), hid_nc=hid("nextcloud"),
        v_nc_folder=e("nc_folder"), v_nc_user=e("nc_user"), v_name=e("name"), v_description=e("description"),
        v_author=e("author"), author_hint="; empty = your Nextcloud name for Nextcloud builds" if NEXTCLOUD_URL else "",
        licenses=opts(licenses.choices, v.get("license") or DEFAULT_LICENSE),
        v_license_description=e("license_description", DEFAULT_LICENSE_DESCRIPTION),
        channels=render_channels()[len("<tr><th>Name</th><th>Version</th><th>Resources</th><th>Author</th></tr>"):],
        port=port, poll_ms=int(os.environ.get("UI_POLL_MS", "1500")))


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
        q = urllib.parse.parse_qs(u.query)
        if p in ("/", "/index.html"):
            job = QUEUE.get(q.get("job", [""])[0])
            values = job.form if job and job.state != DONE else None
            return self._send(200, render(port=self.server.server_address[1], values=values, job=job,
                                     host=self.headers.get("Host", "")),
                              "text/html; charset=utf-8")
        if p == "/api/usb-status":
            return self._json(usb_status())
        if p == "/api/queue":
            return self._json(queue_status())
        m = re.match(r"^/api/jobs/([0-9]+)$", p)
        if m:
            job = QUEUE.get(m.group(1))
            return self._json(job.public(QUEUE.position(job))) if job else self._json({"error": "not found"}, 404)
        if p == "/api/fragments":  # what the page's poller swaps in
            job = QUEUE.get(q.get("job", [""])[0])
            return self._json(dict(queue=render_queue(), usb=render_usb(), job=render_job(job),
                                   channels=render_channels()))
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
            want_json = self.headers.get("Accept", "").startswith("application/json")
            port = self.server.server_address[1]
            try:
                job = submit_build(form)
            except (webdav.AuthError, ValueError, OSError) as e:
                code = 403 if isinstance(e, webdav.AuthError) else 400
                if want_json:
                    return self._json({"error": str(e)}, code)
                # keep what the teacher typed (UI issue 5); never echo the password
                return self._send(code, render('<p class="err">%s</p>' % html.escape(str(e)), port, values=form,
                                         host=self.headers.get("Host", "")),
                                  "text/html; charset=utf-8")
            if want_json:
                if form.get("async") == "1":
                    return self._json(job.public(QUEUE.position(job)), 202)
                QUEUE.wait(job)
                if job.state != DONE:
                    return self._json({"error": job.message}, 400)
                return self._json(job.result)
            # run in the background; the page polls the job's progress (UI issue 1)
            self.send_response(303)
            self.send_header("Location", "/?job=%s" % job.id)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return self._json({"error": "not found"}, 404)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    port = int(os.environ.get("PORT", "8090"))
    os.makedirs(EXPORT_DIR, exist_ok=True)
    QUEUE.start()
    threading.Thread(target=AUTO.loop, name="usb-watch", daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("channel builder on :%d, files=%s usb=%s export=%s nextcloud=%s", port, FILES_ROOT, USB_ROOT, EXPORT_DIR, NEXTCLOUD_URL or "-")
    srv.serve_forever()


if __name__ == "__main__":
    main()

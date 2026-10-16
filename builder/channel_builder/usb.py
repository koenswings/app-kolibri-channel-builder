"""USB sticks (idea#193): read-only sources and automatic builds.

The Engine mounts FAT/exFAT sticks read-only at /run/idea-usb/<dev> and binds
that folder into this container as USB_ROOT (x-app.usbMount, rslave), and
writes USB_ROOT/.idea-usb.json describing the mounted sticks. A stick counts
only once its entry is in that JSON. Nothing is ever written to USB_ROOT;
fingerprints and status live in WORK_DIR.

Auto-build: a stick with a top-level folder named `kolibri` (any case) gets one
channel per subfolder (`kolibri/<channel>`), with optional
`kolibri/<channel>/channel.json` {name, author, description, license,
license_description}. The source id is `usb:<fs-uuid>:<path on the stick>`, so
the same stick folder always updates the same channel.
"""
import hashlib
import json
import logging
import os
import re
import threading

from channel_builder.build import iter_supported

log = logging.getLogger("channel_builder.usb")

STICKS_JSON = ".idea-usb.json"
DEVICE = re.compile(r"^sd[a-z][0-9]?$")
REMOVED_MSG = "USB stick removed before the build finished; plug it in again."


class StickRemoved(Exception):
    pass


def _stick_uuid(e):
    u = e.get("uuid") or ""
    if not u:
        sid = e.get("stickId") or ""
        u = sid[4:] if sid.startswith("usb-") else sid
    return re.sub(r"[^0-9A-Za-z-]", "", u)


def list_sticks(root):
    """Sticks in the Engine's JSON whose mount folder exists.

    Accepts a list or {"sticks": [...]} of UsbStickPublic-like entries:
    stickId ('usb-<uuid>'), device ('sdb1'), label, displayName ('USB: X').
    """
    try:
        with open(os.path.join(root, STICKS_JSON)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("sticks", [])
    out = []
    for e in data if isinstance(data, list) else []:
        if not isinstance(e, dict):
            continue
        dev, uid = str(e.get("device", "")), _stick_uuid(e)
        if not DEVICE.match(dev) or not uid:
            continue
        path = os.path.join(root, dev)
        if not os.path.isdir(path):
            continue
        label = str(e.get("label") or "USB stick")
        out.append(dict(stickId="usb-" + uid, uuid=uid, device=dev, path=path, label=label,
                        displayName=str(e.get("displayName") or "USB: " + label)))
    return out


def find_stick(root, stick_id):
    for s in list_sticks(root):
        if s["stickId"] == stick_id:
            return s
    return None


def stick_folders(stick, max_depth=3):
    res = []
    base = stick["path"]
    depth0 = base.rstrip("/").count("/")
    try:
        for cur, dirs, _ in os.walk(base):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "System Volume Information")
            depth = cur.rstrip("/").count("/") - depth0
            if depth >= max_depth:
                dirs[:] = []
            if depth >= 1:
                res.append(os.path.relpath(cur, base))
    except OSError:
        pass
    return res


def safe_stick_path(stick, rel):
    base = os.path.realpath(stick["path"])
    p = os.path.realpath(os.path.join(base, rel.lstrip("/")))
    if not (p == base or p.startswith(base + os.sep)) or not os.path.isdir(p):
        raise ValueError("folder not found on the USB stick")
    return p


def source_id(stick, rel):
    return "usb:%s:%s" % (stick["uuid"], rel.strip("/"))


def make_check(root, stick):
    """Raises StickRemoved once the stick is gone (JSON entry or mount folder)."""
    def check():
        cur = find_stick(root, stick["stickId"])
        if not cur or cur["device"] != stick["device"] or not os.path.isdir(stick["path"]):
            raise StickRemoved(REMOVED_MSG)
    return check


def kolibri_dir(stick):
    try:
        for e in sorted(os.listdir(stick["path"])):
            p = os.path.join(stick["path"], e)
            if e.lower() == "kolibri" and os.path.isdir(p):
                return e
    except OSError:
        pass
    return None


def channel_folders(stick):
    k = kolibri_dir(stick)
    if not k:
        return []
    base = os.path.join(stick["path"], k)
    return [k + "/" + d for d in sorted(os.listdir(base))
            if not d.startswith(".") and os.path.isdir(os.path.join(base, d))]


def read_channel_json(folder_abs, licence_ids):
    """(metadata dict, warnings). Unknown/bad fields -> defaults + warning."""
    p = os.path.join(folder_abs, "channel.json")
    if not os.path.isfile(p):
        return {}, []
    try:
        with open(p, encoding="utf-8-sig") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as e:
        return {}, ["channel.json ignored (%s); using defaults" % e]
    meta, warns = {}, []
    for k in ("name", "author", "description", "license_description"):
        if isinstance(d.get(k), str) and d[k].strip():
            meta[k] = d[k].strip()
    lang = d.get("language")
    if lang is not None:
        from channel_builder.build import valid_language
        if valid_language(lang):
            meta["language"] = valid_language(lang)
        else:
            warns.append("channel.json: unknown language %r; using the default" % (lang,))
    lic = d.get("license")
    if lic is not None:
        if lic in licence_ids:
            meta["license"] = lic
        else:
            warns.append("channel.json: unknown licence %r; using the default" % (lic,))
    return meta, warns


def fingerprint(folder_abs):
    """Paths + sizes + mtimes of the buildable files and channel.json."""
    h = hashlib.sha256()
    items = list(iter_supported(folder_abs))
    cj = os.path.join(folder_abs, "channel.json")
    if os.path.isfile(cj):
        items.append(("channel.json", cj))
    total = 0
    for rel, p in sorted(items):
        st = os.stat(p)
        total += st.st_size if rel != "channel.json" else 0
        h.update(("%s\0%d\0%d\n" % (rel, st.st_size, st.st_mtime_ns)).encode("utf-8", "surrogateescape"))
    return h.hexdigest(), total


class State(object):
    """Last successful fingerprint per source id, in WORK_DIR (never on the stick)."""

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        try:
            with open(path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def get(self, sid):
        return self.data.get(sid)

    def put(self, sid, fp):
        with self.lock:
            self.data[sid] = fp
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)

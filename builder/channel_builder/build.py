"""Build a Kolibri channel export (content DB + storage) from a folder tree.

Folder = topic, supported file = resource. Output layout is exactly what
`kolibri manage importchannel disk` / `importcontent disk` expect:

  <out>/KOLIBRI_DATA/content/databases/<channel_id>.sqlite3
  <out>/KOLIBRI_DATA/content/storage/<m>/<d>/<md5>.<ext>

The DB schema comes from Kolibri's own SQLAlchemy content schema
(kolibri.core.content.contentschema.versions.content_schema_current), so the
builder must run with the same kolibri version that app-kolibri ships.
"""
import argparse
import datetime
import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import uuid
import zipfile

import kolibri  # noqa: F401  (sets up kolibri.dist on sys.path)
from kolibri.core.content.constants.schema_versions import CONTENT_SCHEMA_VERSION
from kolibri.core.content.contentschema.versions import content_schema_current as schema
from le_utils.constants import content_kinds, format_presets, languages, licenses
from le_utils.constants.labels import learning_activities as la
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

log = logging.getLogger("channel_builder")
NS = uuid.UUID("5b0c3c1e-6a0e-4c0b-9d6e-1dea0c4a1b11")  # IDEA namespace

DEFAULT_LICENSE = licenses.ALL_RIGHTS_RESERVED
DEFAULT_LICENSE_DESCRIPTION = "School use"

VIDEO_CODECS = {"h264", "vp8", "vp9"}
AUDIO_CODECS = {"aac", "mp3", "opus", "vorbis"}
IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "svg"}

# ext -> (kind, preset, learning activity)
TYPES = {
    "pdf": (content_kinds.DOCUMENT, format_presets.DOCUMENT, la.READ),
    "epub": (content_kinds.DOCUMENT, format_presets.EPUB, la.READ),
    "mp4": (content_kinds.VIDEO, format_presets.VIDEO_HIGH_RES, la.WATCH),
    "webm": (content_kinds.VIDEO, format_presets.VIDEO_HIGH_RES, la.WATCH),
    "mp3": (content_kinds.AUDIO, format_presets.AUDIO, la.LISTEN),
}
for _e in IMAGE_EXT:
    TYPES[_e] = (content_kinds.HTML5, format_presets.HTML5_ZIP, la.EXPLORE)
# A real, interactive HTML5 app: a .zip with index.html at its root (Kolibri's
# html5_zip format). Stored unchanged; zips without a root index.html are skipped.
TYPES["zip"] = (content_kinds.HTML5, format_presets.HTML5_ZIP, la.EXPLORE)


FALLBACK_LANGUAGE = "en"


def valid_language(code):
    """Normalised le_utils language id ('en', 'pt-BR', ...) or None if unknown.

    Accepts gettext-style values too ('en_US.UTF-8', 'fr:en' -> first entry)."""
    if not isinstance(code, str):
        return None
    c = code.strip().split(":")[0].split(".")[0].replace("_", "-")
    if not c or c in ("C", "POSIX"):
        return None
    for cand in (c, c.split("-")[0]):
        lang = languages.getlang(cand) or languages.getlang(cand.lower())
        if lang:
            return lang.code if hasattr(lang, "code") else cand
    return None


def default_language():
    """CHANNEL_LANGUAGE / LANGUAGE env (if valid), else 'en'."""
    for var in ("CHANNEL_LANGUAGE", "LANGUAGE"):
        v = valid_language(os.environ.get(var))
        if v:
            return v
    return FALLBACK_LANGUAGE


def language_row(code):
    """content_language row (Kolibri 0.15.5 schema) for a valid language id."""
    lang = languages.getlang(code)
    return dict(id=code, lang_code=lang.primary_code, lang_subcode=lang.subcode,
                lang_name=lang.native_name or lang.name,
                lang_direction=languages.getlang_direction(code))


def uid(*parts):
    return uuid.uuid5(NS, "/".join(parts)).hex


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe(path):
    """Return (duration_s or None, warnings) using ffprobe if available."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name:format=duration", "-of", "json", path],
            capture_output=True, text=True, timeout=60, check=True).stdout
    except (OSError, subprocess.SubprocessError) as e:
        return None, ["could not probe %s: %s" % (path, e)]
    info = json.loads(out)
    warns = []
    for s in info.get("streams", []):
        c, t = s.get("codec_name"), s.get("codec_type")
        if t == "video" and c not in VIDEO_CODECS | {"mjpeg", "png"}:
            warns.append("%s: video codec %s may not play in browsers (use H.264/VP8/VP9)" % (path, c))
        if t == "audio" and c not in AUDIO_CODECS:
            warns.append("%s: audio codec %s may not play in browsers (use AAC/MP3/Opus)" % (path, c))
    d = info.get("format", {}).get("duration")
    return (int(round(float(d))) if d else None), warns


def html5_app_problem(path):
    """None if path is a usable HTML5 app zip (index.html at the root), else why not."""
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "index.html" not in names:
                return "no index.html at the top of the zip"
            bad = z.testzip()
            if bad:
                return "damaged zip (%s)" % bad
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as e:
        return "not a readable zip (%s)" % e
    return None


def supported(path):
    """True if the builder turns this file into a resource."""
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext not in TYPES:
        return False
    if ext == "zip":
        return html5_app_problem(path) is None
    return True


def image_zip(path, title):
    """Wrap an image in a minimal HTML5 zip (Kolibri 0.15 has no image kind)."""
    name = "image" + os.path.splitext(path)[1].lower()
    html = ('<!doctype html><html><head><meta charset="utf-8"><title>{t}</title>'
            '<style>body{{margin:0;background:#222;display:flex;align-items:center;'
            'justify-content:center;height:100vh}}img{{max-width:100%;max-height:100%}}'
            '</style></head><body><img src="{n}" alt="{t}"></body></html>').format(
                t=title.replace("<", ""), n=name)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # fixed timestamps -> deterministic md5 across rebuilds
        for arc, data in (("index.html", html.encode()), (name, open(path, "rb").read())):
            zi = zipfile.ZipInfo(arc, (2020, 1, 1, 0, 0, 0))
            z.writestr(zi, data)
    return buf.getvalue()


def iter_supported(src):
    """(relpath, abspath) of every file the builder would turn into a resource."""
    for cur, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for f in sorted(files):
            if f.startswith("."):
                continue
            p = os.path.join(cur, f)
            if supported(p):
                yield os.path.relpath(p, src), p


def count_supported(src):
    return sum(1 for _ in iter_supported(src))


class Builder(object):
    def __init__(self, src, out, name, author, source_id=None, description="",
                 license_name=DEFAULT_LICENSE, license_description=DEFAULT_LICENSE_DESCRIPTION,
                 version=None, lang=None, progress=None, check=None):
        self.src = os.path.abspath(src)
        self.out = os.path.abspath(out)
        self.name, self.author, self.description = name, author, description
        self.license_name, self.license_description = license_name, license_description
        self.source_id = source_id or self.src
        self.channel_id = uid("channel", self.source_id)
        self.version = version
        # Every channel needs a language: Kolibri 0.15.5's html5_viewer reads
        # contentNode.lang.id and crashes when lang is null.
        self.lang = valid_language(lang) or default_language()
        # progress(done, total) after each resource; check() before each file
        # (raises to abort, e.g. when a USB stick was pulled out).
        self.progress, self.check = progress, check
        self.total, self.done = 0, 0
        self.warnings, self.skipped = [], []
        self.nodes, self.files, self.localfiles = [], [], {}
        self.storage = os.path.join(self.out, "KOLIBRI_DATA", "content", "storage")
        self.dbdir = os.path.join(self.out, "KOLIBRI_DATA", "content", "databases")

    # -- storage ----------------------------------------------------------
    def _store_bytes(self, data, ext):
        md5 = hashlib.md5(data).hexdigest()
        dest = self._dest(md5, ext)
        if not os.path.exists(dest):
            with open(dest + ".tmp", "wb") as f:
                f.write(data)
            os.replace(dest + ".tmp", dest)
        self.localfiles[md5] = (ext, len(data))
        return md5

    def _store_path(self, path, ext):
        md5 = md5_file(path)
        dest = self._dest(md5, ext)
        if not os.path.exists(dest):
            shutil.copyfile(path, dest + ".tmp")
            os.replace(dest + ".tmp", dest)
        self.localfiles[md5] = (ext, os.path.getsize(path))
        return md5

    def _dest(self, md5, ext):
        d = os.path.join(self.storage, md5[0], md5[1])
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "%s.%s" % (md5, ext))

    # -- tree -------------------------------------------------------------
    def _node(self, rel, title, kind, parent, level, ancestors, sort, **kw):
        nid = self.channel_id if parent is None else uid(self.channel_id, "node", rel)
        n = dict(id=nid, title=title[:200], content_id=uid(self.source_id, "content", rel),
                 channel_id=self.channel_id, description="", sort_order=float(sort),
                 license_owner=self.author[:200], author=self.author[:200], kind=kind,
                 available=False, lft=0, rght=0, tree_id=1, level=level,
                 lang_id=self.lang, coach_content=False, options="{}",
                 ancestors=json.dumps(ancestors), parent_id=parent,
                 license_name=None if kind == content_kinds.TOPIC else self.license_name,
                 license_description=None if kind == content_kinds.TOPIC else self.license_description,
                 learning_activities=kw.pop("activity", None), duration=kw.pop("duration", None))
        n.update(kw)
        self.nodes.append(n)
        return n

    def _walk(self, absdir, rel, parent, level, ancestors):
        entries = sorted(e for e in os.listdir(absdir) if not e.startswith("."))
        sort = 0
        for e in entries:
            p = os.path.join(absdir, e)
            r = os.path.join(rel, e) if rel else e
            title = os.path.splitext(e)[0].replace("_", " ").strip() or e
            if os.path.isdir(p):
                sort += 1
                t = self._node(r, e, content_kinds.TOPIC, parent["id"], level, ancestors, sort)
                kids_before = len(self.nodes)
                self._walk(p, r, t, level + 1, ancestors + [{"id": t["id"], "title": t["title"]}])
                if len(self.nodes) == kids_before:  # empty topic -> drop
                    self.nodes.remove(t)
                    sort -= 1
                continue
            ext = os.path.splitext(e)[1].lower().lstrip(".")
            if ext not in TYPES:
                self.skipped.append(r)
                continue
            if ext == "zip":
                why = html5_app_problem(p)
                if why:
                    self.skipped.append(r)
                    self.warnings.append("%s: skipped, %s (an HTML5 app zip needs index.html "
                                         "at its root)" % (r, why))
                    continue
            if self.check:
                self.check()
            kind, preset, activity = TYPES[ext]
            duration = None
            if kind in (content_kinds.VIDEO, content_kinds.AUDIO):
                duration, w = probe(p)
                self.warnings.extend(x.replace(p, r) for x in w)
            if kind == content_kinds.HTML5 and ext != "zip":
                md5, fext = self._store_bytes(image_zip(p, title), "zip"), "zip"
            else:
                md5, fext = self._store_path(p, ext), ext
            sort += 1
            n = self._node(r, title, kind, parent["id"], level, ancestors, sort,
                           activity=activity, duration=duration)
            self.done += 1
            if self.progress:
                self.progress(self.done, self.total)
            self.files.append(dict(id=uid(self.channel_id, "file", r, md5), supplementary=False,
                                   thumbnail=False, priority=1, contentnode_id=n["id"],
                                   lang_id=self.lang, local_file_id=md5, preset=preset))

    def _mptt(self):
        kids = {}
        for n in self.nodes:
            kids.setdefault(n["parent_id"], []).append(n)
        counter = [1]

        def visit(n):
            n["lft"] = counter[0]
            counter[0] += 1
            for c in sorted(kids.get(n["id"], []), key=lambda x: x["sort_order"]):
                visit(c)
            n["rght"] = counter[0]
            counter[0] += 1
        visit(self.nodes[0])

    def build(self):
        os.makedirs(self.storage, exist_ok=True)
        os.makedirs(self.dbdir, exist_ok=True)
        self.total = count_supported(self.src)
        if self.progress:
            self.progress(0, self.total)
        root = self._node("", self.name, content_kinds.TOPIC, None, 0, [], 0)
        root["description"] = self.description
        self._walk(self.src, "", root, 1, [{"id": root["id"], "title": root["title"]}])
        resources = [n for n in self.nodes if n["kind"] != content_kinds.TOPIC]
        if not resources:
            raise SystemExit("no supported files found in %s" % self.src)
        self._mptt()
        dbpath = os.path.join(self.dbdir, self.channel_id + ".sqlite3")
        version = self.version
        if version is None:  # bump on every rebuild of the same channel
            version = self._previous_version(dbpath) + 1
        tmp = dbpath + ".tmp"
        if os.path.exists(tmp):
            os.remove(tmp)
        engine = create_engine("sqlite:///" + tmp)
        schema.metadata.create_all(engine)
        S = sessionmaker(bind=engine)()
        S.add(schema.ContentLanguage(**language_row(self.lang)))
        for md5, (ext, size) in self.localfiles.items():
            S.add(schema.ContentLocalfile(id=md5, available=False, file_size=size, extension=ext))
        S.flush()
        for n in self.nodes:  # parents first (walk order)
            S.add(schema.ContentContentnode(**n))
            S.flush()
        for f in self.files:
            S.add(schema.ContentFile(**f))
        S.add(schema.ContentChannelmetadata(
            id=self.channel_id, name=self.name[:200], description=self.description[:400],
            author=self.author[:400], version=version, thumbnail="",
            last_updated=datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f"),
            min_schema_version=CONTENT_SCHEMA_VERSION, root_id=self.channel_id,
            published_size=sum(s for _, s in self.localfiles.values()),
            total_resource_count=len(resources), public=False, tagline=None))
        # 0.15.5 channel metadata has no lang_id column; the channel language is
        # the root node's lang plus included_languages.
        S.add(schema.ContentChannelmetadataIncludedLanguages(
            channelmetadata_id=self.channel_id, language_id=self.lang))
        S.commit()
        S.close()
        engine.dispose()
        os.replace(tmp, dbpath)
        return dict(channel_id=self.channel_id, version=version, db=dbpath, language=self.lang,
                    resources=len(resources), topics=len(self.nodes) - len(resources),
                    files=len(self.localfiles), warnings=self.warnings, skipped=self.skipped)

    @staticmethod
    def _previous_version(dbpath):
        if not os.path.exists(dbpath):
            return 0
        import sqlite3
        try:
            c = sqlite3.connect(dbpath)
            v = c.execute("select max(version) from content_channelmetadata").fetchone()[0]
            c.close()
            return int(v or 0)
        except sqlite3.Error:
            return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("src", help="folder to turn into a channel")
    ap.add_argument("out", help="export dir (KOLIBRI_DATA is created inside)")
    ap.add_argument("--name", required=True)
    ap.add_argument("--author", required=True, help="teacher name")
    ap.add_argument("--description", default="")
    ap.add_argument("--source-id", help="stable id (default: absolute src path)")
    ap.add_argument("--license", default=DEFAULT_LICENSE)
    ap.add_argument("--license-description", default=DEFAULT_LICENSE_DESCRIPTION)
    ap.add_argument("--version", type=int)
    ap.add_argument("--language", help="channel language (default: $LANGUAGE or en)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    r = Builder(a.src, a.out, a.name, a.author, a.source_id, a.description,
                a.license, a.license_description, a.version, a.language).build()
    for w in r["warnings"]:
        log.warning(w)
    for s in r["skipped"]:
        log.info("skipped unsupported file: %s", s)
    print(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

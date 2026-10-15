import os
import sqlite3

import pytest

from channel_builder.build import Builder, DEFAULT_LICENSE


def build(src, out, **kw):
    kw.setdefault("name", "Test")
    kw.setdefault("author", "Ms Teacher")
    return Builder(str(src), str(out), source_id="test", **kw).build()


def rows(db, sql):
    c = sqlite3.connect(db)
    r = c.execute(sql).fetchall()
    c.close()
    return r


def test_tree_kinds_and_skips(samples, tmp_path):
    r = build(samples, tmp_path)
    assert r["resources"] == 6  # pdf, epub, 2x mp4, mp3, png
    assert r["skipped"] == ["Music/notes.docx"]
    kinds = sorted(k for (k,) in rows(r["db"], "select kind from content_contentnode where kind!='topic'"))
    assert kinds == ["audio", "document", "document", "html5", "video", "video"]
    titles = [t for (t,) in rows(r["db"], "select title from content_contentnode where kind='topic'")]
    assert "Empty" not in titles and ".hidden" not in titles


def test_unsupported_codec_warns(samples, tmp_path):
    r = build(samples, tmp_path)
    assert any("Old_codec.mp4" in w and "mpeg4" in w for w in r["warnings"])
    assert not any("Lesson_1" in w for w in r["warnings"])
    assert all(not w.startswith("/") for w in r["warnings"])  # no host paths leaked


def test_defaults_licence_author(samples, tmp_path):
    r = build(samples, tmp_path)
    lic = set(rows(r["db"], "select license_name, license_description, author from content_contentnode where kind!='topic'"))
    assert lic == {(DEFAULT_LICENSE, "School use", "Ms Teacher")}


def test_licence_editable(samples, tmp_path):
    r = build(samples, tmp_path, license_name="CC BY", license_description="")
    assert {l for (l,) in rows(r["db"], "select license_name from content_contentnode where kind!='topic'")} == {"CC BY"}


def test_mptt_consistent(samples, tmp_path):
    r = build(samples, tmp_path)
    nodes = rows(r["db"], "select id,parent_id,lft,rght,level from content_contentnode")
    by = {n[0]: n for n in nodes}
    lr = sorted(v for n in nodes for v in (n[2], n[3]))
    assert lr == list(range(1, 2 * len(nodes) + 1))
    for n in nodes:
        if n[1]:
            p = by[n[1]]
            assert p[2] < n[2] < n[3] < p[3] and n[4] == p[4] + 1


def test_storage_layout_and_md5(samples, tmp_path):
    import hashlib
    r = build(samples, tmp_path)
    for md5, ext in rows(r["db"], "select id, extension from content_localfile"):
        p = tmp_path / "KOLIBRI_DATA" / "content" / "storage" / md5[0] / md5[1] / ("%s.%s" % (md5, ext))
        assert hashlib.md5(p.read_bytes()).hexdigest() == md5


def test_rebuild_same_id_bumps_version_and_is_deterministic(samples, tmp_path):
    a = build(samples, tmp_path)
    b = build(samples, tmp_path)
    assert a["channel_id"] == b["channel_id"] and b["version"] == a["version"] + 1
    assert a["files"] == b["files"]
    assert len(os.listdir(tmp_path / "KOLIBRI_DATA" / "content" / "databases")) == 1


def test_empty_folder_fails(tmp_path):
    (tmp_path / "src").mkdir()
    with pytest.raises(SystemExit):
        build(tmp_path / "src", tmp_path / "out")

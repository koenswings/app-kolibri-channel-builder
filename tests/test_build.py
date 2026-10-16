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


# -- language (Kolibri 0.15.5 html5_viewer reads contentNode.lang.id) ---------

def _langs(db):
    return dict(
        languages=rows(db, "select id, lang_code, lang_direction from content_language"),
        nodes=set(l for (l,) in rows(db, "select lang_id from content_contentnode")),
        files=set(l for (l,) in rows(db, "select lang_id from content_file")),
        root=rows(db, "select n.lang_id from content_contentnode n join content_channelmetadata c "
                      "on c.root_id = n.id")[0][0],
        included=rows(db, "select language_id from content_channelmetadata_included_languages"))


def test_language_defaults_to_en(samples, tmp_path, monkeypatch):
    monkeypatch.delenv("CHANNEL_LANGUAGE", raising=False)
    monkeypatch.delenv("LANGUAGE", raising=False)
    r = build(samples, tmp_path)
    L = _langs(r["db"])
    assert r["language"] == "en"
    assert L["languages"] == [("en", "en", "ltr")]
    assert L["nodes"] == L["files"] == {"en"} and L["root"] == "en"
    assert L["included"] == [("en",)]


def test_language_from_env_and_argument(samples, tmp_path, monkeypatch):
    monkeypatch.delenv("CHANNEL_LANGUAGE", raising=False)
    monkeypatch.setenv("LANGUAGE", "fr_FR.UTF-8:en")
    assert _langs(build(samples, tmp_path / "a")["db"])["nodes"] == {"fr"}
    monkeypatch.setenv("LANGUAGE", "not-a-language")
    assert _langs(build(samples, tmp_path / "b")["db"])["nodes"] == {"en"}
    r = build(samples, tmp_path / "c", lang="pt-BR")
    assert _langs(r["db"])["languages"] == [("pt-BR", "pt", "ltr")]
    r = build(samples, tmp_path / "d", lang="bogus")  # invalid -> default, never null
    assert _langs(r["db"])["nodes"] == {"en"}


def test_channel_json_language(tmp_path):
    import json
    from channel_builder import usb
    from le_utils.constants import licenses
    (tmp_path / "channel.json").write_text(json.dumps({"language": "sn"}))
    assert usb.read_channel_json(str(tmp_path), dict(licenses.choices)) == ({"language": "sn"}, [])
    (tmp_path / "channel.json").write_text(json.dumps({"language": "klingon"}))
    meta, warns = usb.read_channel_json(str(tmp_path), dict(licenses.choices))
    assert "language" not in meta and "unknown language" in warns[0]

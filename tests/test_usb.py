"""idea#193 step 4 + 4b: USB source, auto-build, and the five UI fixes."""
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

UUID_A, UUID_B = "1A2B-3C4D", "5E6F-7A8B"


def pdf(fu):
    return fu.samples / "Science" / "Reading" / "Worksheet.pdf"


def png(fu):
    return fu.samples / "Science" / "Diagram.png"


def kolibri_stick(fu, device="sdb1", uuid=UUID_A, label="MARY-STICK", extra=None):
    tree = {"kolibri/Grade 5 Science/Plants/Leaf.pdf": pdf(fu),
            "kolibri/Grade 5 Science/Plants/Leaf.png": png(fu),
            "kolibri/Grade 6 Maths/Fractions.pdf": pdf(fu),
            "Holiday/photo.png": png(fu)}
    tree.update(extra or {})
    return fu.plug(device, uuid, label, tree)


def wait_idle(s, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        q = s.QUEUE.snapshot()
        if not q["running"] and not q["queued"]:
            return
        time.sleep(0.05)
    raise AssertionError("queue did not drain")


def db_version(s, cid):
    c = sqlite3.connect(os.path.join(s.kdata(), "databases", cid + ".sqlite3"))
    r = c.execute("select version, name, author from content_channelmetadata").fetchone()
    c.close()
    return r


def channel(status, name):
    for st in status["sticks"]:
        for c in st["channels"]:
            if c["name"] == name:
                return st, c
    raise KeyError(name)


def post(url, data, json_=True):
    h = {"Accept": "application/json"} if json_ else {}
    return urllib.request.urlopen(urllib.request.Request(url, urllib.parse.urlencode(data).encode(), headers=h))


# -- step 4: USB as a manual source -------------------------------------

def test_sticks_from_engine_json_only(builder_env):
    s, fu = builder_env
    kolibri_stick(fu)
    (fu.root / "sdc1").mkdir()  # mounted folder without a JSON entry: not a stick (yet)
    sticks = s.usb.list_sticks(str(fu.root))
    assert [x["stickId"] for x in sticks] == ["usb-" + UUID_A]
    assert sticks[0]["displayName"] == "USB: MARY-STICK"
    assert "kolibri/Grade 5 Science" in s.usb.stick_folders(sticks[0])


def test_both_roots_in_form(builder_env):
    s, fu = builder_env
    kolibri_stick(fu)
    page = s.render()
    assert "disk1/Science" in page and "USB: MARY-STICK / kolibri/Grade 6 Maths" in page
    assert '<option value="usb">USB: MARY-STICK</option>' in page


@pytest.mark.parametrize("rel", ["../../etc", "/../..", "kolibri/../../../etc", "escape"])
def test_usb_path_escape_refused(builder_env, rel):
    s, fu = builder_env
    d = kolibri_stick(fu)
    (d / "escape").symlink_to("/etc")
    with pytest.raises(ValueError):
        s.prepare_build(dict(source="usb", usb_folder="usb-%s/%s" % (UUID_A, rel), name="x", author="T"))


def test_stable_channel_id_from_uuid_and_path(builder_env):
    s, fu = builder_env
    kolibri_stick(fu)
    f = dict(source="usb", usb_folder="usb-%s/kolibri/Grade 6 Maths" % UUID_A, name="Maths", author="T")
    r1 = s.build_from_form(f)
    # same stick in another port (different device name) -> same channel, next version
    fu.unplug("sdb1", keep_files=True)
    fu.root.joinpath(".sdb1-pulled").rename(fu.root / "sdc1")
    fu.plug("sdc1", UUID_A, "MARY-STICK")
    r2 = s.build_from_form(f)
    assert r1["channel_id"] == r2["channel_id"] and (r1["version"], r2["version"]) == (1, 2)
    from channel_builder.build import uid
    assert r1["channel_id"] == uid("channel", "usb:%s:kolibri/Grade 6 Maths" % UUID_A)
    # a different stick with the same folder is a different channel
    kolibri_stick(fu, "sdd1", UUID_B, "OTHER")
    r3 = s.build_from_form(dict(f, usb_folder="usb-%s/kolibri/Grade 6 Maths" % UUID_B))
    assert r3["channel_id"] != r1["channel_id"]


def test_manual_usb_build_pulled_mid_build_fails_cleanly(builder_env, monkeypatch):
    s, fu = builder_env
    kolibri_stick(fu)
    f = dict(source="usb", usb_folder="usb-%s/kolibri/Grade 5 Science" % UUID_A, name="Sci", author="T")
    r1 = s.build_from_form(f)
    real = s.usb.make_check

    def pulling(root, stick):
        chk, n = real(root, stick), [0]

        def check():
            n[0] += 1
            if n[0] == 2:
                fu.unplug("sdb1")
            chk()
        return check
    monkeypatch.setattr(s.usb, "make_check", pulling)
    with pytest.raises(ValueError, match="USB stick removed before the build finished"):
        s.build_from_form(f)
    assert db_version(s, r1["channel_id"])[0] == 1  # previous version intact and served
    assert not [x for x in os.listdir(os.path.join(s.kdata(), "databases")) if x.endswith(".tmp")]


def test_nothing_written_to_stick(builder_env):
    s, fu = builder_env
    d = kolibri_stick(fu)
    before = sorted((str(p), p.stat().st_mtime_ns) for p in d.rglob("*"))
    os.chmod(str(d), 0o555)
    try:
        s.AUTO.poll()
        wait_idle(s)
    finally:
        os.chmod(str(d), 0o755)
    assert sorted((str(p), p.stat().st_mtime_ns) for p in d.rglob("*")) == before
    assert all(c["state"] == "done" for c in s.usb_status()["sticks"][0]["channels"])


# -- step 4b: auto-build -------------------------------------------------

def test_autobuild_one_channel_per_kolibri_subfolder_in_order(builder_env):
    s, fu = builder_env
    kolibri_stick(fu)
    s.AUTO.poll()
    titles = [j["title"] for j in s.QUEUE.snapshot()["running"] + s.QUEUE.snapshot()["queued"]]
    assert titles == ["Grade 5 Science (USB: MARY-STICK)", "Grade 6 Maths (USB: MARY-STICK)"]
    wait_idle(s)
    st = s.usb_status()
    assert st["sticks"][0]["state"] == "built"
    assert st["sticks"][0]["message"] == "Built: you can pull the stick out."
    names = sorted(c["name"] for c in s.list_channels())
    assert names == ["Grade 5 Science", "Grade 6 Maths"]  # Holiday/ is never built
    assert {c["author"] for c in s.list_channels()} == {"MARY-STICK"}  # default author = stick label


def test_kolibri_folder_case_insensitive(builder_env):
    s, fu = builder_env
    fu.plug("sdb1", UUID_A, "X", {"KOLIBRI/Topic/a.pdf": pdf(fu)})
    s.AUTO.poll()
    wait_idle(s)
    assert [c["name"] for c in s.list_channels()] == ["Topic"]


def test_stick_without_kolibri_folder_never_built(builder_env):
    s, fu = builder_env
    fu.plug("sdb1", UUID_A, "PHOTOS", {"Holiday/a.png": png(fu), "doc.pdf": pdf(fu)})
    s.AUTO.poll()
    assert s.QUEUE.snapshot()["queued"] == [] and s.QUEUE.snapshot()["running"] == []
    st = s.usb_status()["sticks"][0]
    assert st["state"] == "none" and st["channels"] == []


def test_reinsert_unchanged_skips_changed_rebuilds(builder_env):
    s, fu = builder_env
    d = kolibri_stick(fu)
    s.AUTO.poll()
    wait_idle(s)
    ids = {c["name"]: c["id"] for c in s.list_channels()}
    fu.unplug("sdb1", keep_files=True)
    s.AUTO.poll()
    fu.replug("sdb1", UUID_A, "MARY-STICK")
    s.AUTO.poll()
    assert s.QUEUE.snapshot()["queued"] == [] and s.QUEUE.snapshot()["running"] == []
    assert {c["state"] for c in s.usb_status()["sticks"][0]["channels"]} == {"skipped"}
    # one file changed in one channel -> only that channel rebuilt, version +1, same id
    fu.unplug("sdb1", keep_files=True)
    s.AUTO.poll()
    fu.replug("sdb1", UUID_A, "MARY-STICK")
    (d / "kolibri/Grade 6 Maths/More.pdf").write_bytes(pdf(fu).read_bytes() + b"\n%extra")
    s.AUTO.poll()
    wait_idle(s)
    st = {c["name"]: c["state"] for c in s.usb_status()["sticks"][0]["channels"]}
    assert st == {"Grade 5 Science": "skipped", "Grade 6 Maths": "done"}
    assert db_version(s, ids["Grade 6 Maths"])[0] == 2
    assert db_version(s, ids["Grade 5 Science"])[0] == 1


def test_autobuild_pulled_mid_build_keeps_old_and_rebuilds_on_reinsert(builder_env, monkeypatch):
    s, fu = builder_env
    kolibri_stick(fu)
    s.AUTO.poll()
    wait_idle(s)
    cid = {c["name"]: c["id"] for c in s.list_channels()}["Grade 5 Science"]
    d = fu.root / "sdb1"
    fu.unplug("sdb1", keep_files=True)
    s.AUTO.poll()
    fu.replug("sdb1", UUID_A, "MARY-STICK")
    (d / "kolibri/Grade 5 Science/New.pdf").write_bytes(pdf(fu).read_bytes() + b"\n%new")
    real = s.usb.make_check

    def pulling(root, stick):
        chk, n = real(root, stick), [0]

        def check():
            n[0] += 1
            if n[0] == 2:
                fu.unplug("sdb1", keep_files=True)
            chk()
        return check
    monkeypatch.setattr(s.usb, "make_check", pulling)
    s.AUTO.poll()
    wait_idle(s)
    stick, ch = channel(s.usb_status(), "Grade 5 Science")
    assert ch["state"] == "failed" and "plug it in again" in ch["message"]
    assert stick["state"] == "failed"
    assert db_version(s, cid)[0] == 1  # old version still served
    monkeypatch.setattr(s.usb, "make_check", real)
    s.AUTO.poll()
    fu.replug("sdb1", UUID_A, "MARY-STICK")
    s.AUTO.poll()
    wait_idle(s)
    assert channel(s.usb_status(), "Grade 5 Science")[1]["state"] == "done"
    assert db_version(s, cid)[0] == 2


def test_manual_build_waits_for_autobuild_and_shows_queued(builder_env, http):
    s, fu = builder_env
    kolibri_stick(fu)
    s.BUILD_LOCK.acquire()  # hold the worker inside the first auto-build
    try:
        s.AUTO.poll()
        r = json.load(post(http + "/build", dict(source="files", folder="disk1/Music", name="Songs",
                                                 author="T", **{"async": "1"})))
        assert r["state"] == "queued" and r["position"] == 3
        page = urllib.request.urlopen(http + "/").read().decode()  # a second teacher opens the page
        assert "Songs (Files Disk: disk1 / Music)" in page and "Queued (number 3 in line)" in page
        assert json.load(urllib.request.urlopen(http + "/api/jobs/" + r["id"]))["state"] == "queued"
    finally:
        s.BUILD_LOCK.release()
    wait_idle(s)
    order = [j["title"] for j in reversed(s.QUEUE.snapshot()["finished"])]
    assert order[-1].startswith("Songs") and len(order) == 3


def test_size_cap(builder_env, monkeypatch):
    s, fu = builder_env
    kolibri_stick(fu)
    monkeypatch.setattr(s, "AUTO_BUILD_MAX_BYTES", 1000)
    s.AUTO.poll()
    st = s.usb_status()["sticks"][0]
    assert st["state"] == "too_large" and "use the manual build" in st["message"]
    assert s.QUEUE.snapshot()["queued"] == [] and s.QUEUE.snapshot()["running"] == []


def test_default_cap_is_8_gb():
    from channel_builder import server as s
    assert s.AUTO_BUILD_MAX_BYTES == 8 * 1024 ** 3


def test_autobuild_switch_off(builder_env, monkeypatch):
    s, fu = builder_env
    kolibri_stick(fu)
    monkeypatch.setattr(s, "AUTO_BUILD", False)
    s.AUTO.poll()
    assert s.usb_status()["sticks"][0]["state"] == "off"
    assert not s.QUEUE.snapshot()["queued"]


def test_channel_json_used_and_bad_json_defaults(builder_env):
    s, fu = builder_env
    meta = json.dumps(dict(name="Plants and Water", author="Mary Okello", description="Term 2",
                           license="CC BY", license_description="")).encode()
    kolibri_stick(fu, extra={"kolibri/Grade 5 Science/channel.json": meta,
                             "kolibri/Grade 6 Maths/channel.json": b"{not json",
                             "kolibri/Art/channel.json": json.dumps({"license": "Free"}).encode(),
                             "kolibri/Art/a.png": png(fu)})
    s.AUTO.poll()
    wait_idle(s)
    chans = {c["name"]: c for c in s.list_channels()}
    assert set(chans) == {"Plants and Water", "Grade 6 Maths", "Art"}
    assert chans["Plants and Water"]["author"] == "Mary Okello"
    assert chans["Plants and Water"]["description"] == "Term 2"
    assert chans["Grade 6 Maths"]["author"] == "MARY-STICK"
    st = s.usb_status()
    assert any("channel.json ignored" in w for w in channel(st, "Grade 6 Maths")[1]["warnings"])
    assert any("unknown licence" in w for w in channel(st, "Art")[1]["warnings"])
    c = sqlite3.connect(os.path.join(s.kdata(), "databases", chans["Plants and Water"]["id"] + ".sqlite3"))
    assert {r[0] for r in c.execute("select license_name from content_contentnode where kind!='topic'")} == {"CC BY"}


def test_usb_status_api(builder_env, http):
    s, fu = builder_env
    kolibri_stick(fu)
    s.AUTO.poll()
    wait_idle(s)
    st = json.load(urllib.request.urlopen(http + "/api/usb-status"))
    assert st["autoBuild"] is True and st["maxBytes"] == 8 * 1024 ** 3
    stick = st["sticks"][0]
    assert stick["stickId"] == "usb-" + UUID_A and stick["present"] is True and stick["state"] == "built"
    c = stick["channels"][0]
    assert c["folder"] == "kolibri/Grade 5 Science" and c["done"] == c["total"] == 2 and c["version"] == 1
    fu.unplug("sdb1")
    s.AUTO.poll()
    assert json.load(urllib.request.urlopen(http + "/api/usb-status"))["sticks"][0]["present"] is False


def test_watcher_thread_picks_up_stick(builder_env, monkeypatch):
    s, fu = builder_env
    monkeypatch.setattr(s, "USB_POLL_SECONDS", 0.1)
    threading.Thread(target=s.AUTO.loop, daemon=True).start()
    kolibri_stick(fu)
    end = time.time() + 30
    while time.time() < end and len(s.list_channels()) < 2:
        time.sleep(0.1)
    assert len(s.list_channels()) == 2


# -- UI fixes ------------------------------------------------------------

def test_ui1_html_build_runs_in_background_with_progress(builder_env, http):
    s, fu = builder_env

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    s.BUILD_LOCK.acquire()
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            opener.open(urllib.request.Request(http + "/build", urllib.parse.urlencode(
                dict(source="files", folder="disk1/Science", name="Sci", author="T")).encode()))
        assert e.value.code == 303
        loc = e.value.headers["Location"]
        assert loc.startswith("/?job=")
        job = loc.split("=")[1]
        assert json.load(urllib.request.urlopen(http + "/api/jobs/" + job))["state"] == "running"
        page = urllib.request.urlopen(http + loc).read().decode()
        assert "Your build: Sci" in page and "/api/fragments" in page
    finally:
        s.BUILD_LOCK.release()
    wait_idle(s)
    j = json.load(urllib.request.urlopen(http + "/api/jobs/" + job))
    assert j["state"] == "done" and j["done"] == j["total"] == 5
    frag = json.load(urllib.request.urlopen(http + "/api/fragments?job=" + job))
    assert "Built <b>Sci</b> version 1" in frag["job"]


def test_ui1_progress_callback_counts(builder_env):
    from channel_builder.build import Builder
    s, fu = builder_env
    seen = []
    Builder(str(fu.samples), s.EXPORT_DIR, "All", "T", "t", progress=lambda d, t: seen.append((d, t))).build()
    assert seen[0] == (0, 6) and seen[-1] == (6, 6)


def test_ui3_only_chosen_source_field_shown(builder_env, monkeypatch):
    s, fu = builder_env
    monkeypatch.setattr(s, "NEXTCLOUD_URL", "http://nc.invalid")
    kolibri_stick(fu)
    page = s.render(values={"source": "usb"})
    assert '<div data-src="usb">' in page
    assert '<div data-src="files" hidden>' in page and '<div data-src="nextcloud" hidden id="nc-fields">' in page
    page = s.render()
    assert '<div data-src="files">' in page and '<div data-src="usb" hidden>' in page


def test_ui4_no_login_for_files_or_usb_when_nextcloud_set(builder_env, monkeypatch):
    s, fu = builder_env
    monkeypatch.setattr(s, "NEXTCLOUD_URL", "http://127.0.0.1:9")  # unreachable: any login attempt fails
    kolibri_stick(fu)
    assert s.build_from_form(dict(source="files", folder="disk1/Music", name="M", author="T"))["resources"] == 1
    assert s.build_from_form(dict(source="usb", usb_folder="usb-%s/kolibri/Grade 6 Maths" % UUID_A,
                                  name="M2", author="T"))["resources"] == 1
    with pytest.raises(Exception):
        s.prepare_build(dict(source="nextcloud", nc_folder="Docs", name="N", nc_user="u", nc_password="p"))


def test_ui4_default_no_login_env():
    import importlib
    import os as _os
    from channel_builder import server as s
    old = dict(_os.environ)
    try:
        _os.environ["NEXTCLOUD_URL"] = "http://nc"
        _os.environ.pop("REQUIRE_NEXTCLOUD_LOGIN", None)
        importlib.reload(s)
        assert s.REQUIRE_LOGIN is False
    finally:
        _os.environ.clear()
        _os.environ.update(old)
        importlib.reload(s)


def test_ui5_form_values_kept_after_error(builder_env, http):
    s, fu = builder_env
    with pytest.raises(urllib.error.HTTPError) as e:
        post(http + "/build", dict(source="files", folder="disk1/Music", name="", description="Term <2>",
                                   author="Ms Achieng", license="CC BY", license_description="ours"), json_=False)
    assert e.value.code == 400
    page = e.value.read().decode()
    assert "Channel name is required." in page
    assert 'value="Ms Achieng"' in page and "Term &lt;2&gt;" in page and 'value="ours"' in page
    assert '<option value="disk1/Music" selected>' in page and '<option value="CC BY" selected>' in page


def test_ui5_values_kept_after_failed_background_build(builder_env, http):
    s, fu = builder_env
    (fu.root.parent / "files" / "disk1" / "Nothing").mkdir()
    (fu.root.parent / "files" / "disk1" / "Nothing" / "x.docx").write_text("x")
    job = s.submit_build(dict(source="files", folder="disk1/Nothing", name="Empty one", author="Ms A",
                              nc_password="secret"))
    wait_idle(s)
    page = urllib.request.urlopen(http + "/?job=" + job.id).read().decode()
    assert "Failed: no supported files" in page
    assert 'value="Empty one"' in page and 'value="Ms A"' in page and "secret" not in page


# -- wh findings (Atlas) ---------------------------------------------------

def test_kolibri_address_is_an_ip(builder_env, monkeypatch):
    s, fu = builder_env
    assert s.kolibri_address(8090, "192.168.1.20:8090")[0] == "http://192.168.1.20:8090"
    addr, hint = s.kolibri_address(8090, "wizardly-hugle.local:8090")
    assert ".local" not in addr and "IP address" in addr and "Kolibri cannot look up .local" in hint
    monkeypatch.setattr(s, "LAN_IP", "10.0.0.5")
    assert s.kolibri_address(8090, "wizardly-hugle.local:8090")[0] == "http://10.0.0.5:8090"
    assert "http://10.0.0.5:8090" in s.render(port=8090)


def test_empty_state_without_sources(builder_env, monkeypatch, tmp_path):
    s, fu = builder_env
    assert 'id="empty"' not in s.render()
    monkeypatch.setattr(s, "FILES_ROOT", str(tmp_path / "nothing"))
    page = s.render()
    assert 'id="empty"' in page and "No folders to build from yet" in page and 'class="err"' not in page
    kolibri_stick(fu)
    assert 'id="empty"' not in s.render()


# -- idea04 e2e findings -------------------------------------------------------

def test_kolibri_address_uses_public_port(builder_env, monkeypatch):
    s, fu = builder_env
    monkeypatch.setattr(s, "PUBLIC_PORT", "18400")
    assert s.kolibri_address(8090, "192.168.1.20:18400")[0] == "http://192.168.1.20:18400"
    assert s.kolibri_address(8090, "192.168.1.20")[0] == "http://192.168.1.20:18400"
    monkeypatch.setattr(s, "LAN_IP", "10.0.0.5")
    page = s.render(port=8090)
    assert "http://10.0.0.5:18400" in page and ":8090" not in page


def test_kolibri_address_port_from_host_header_without_public_port(builder_env, monkeypatch):
    s, fu = builder_env
    monkeypatch.setattr(s, "PUBLIC_PORT", "")
    assert s.kolibri_address(8090, "192.168.1.20:18400")[0] == "http://192.168.1.20:18400"
    assert s.kolibri_address(8090, "192.168.1.20")[0] == "http://192.168.1.20:8090"


def test_compose_passes_public_port():
    import os
    root = os.path.join(os.path.dirname(__file__), "..")
    assert "PUBLIC_PORT=${port}" in open(os.path.join(root, "compose.yaml")).read()

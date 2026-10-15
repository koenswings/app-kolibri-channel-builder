"""Source/folder picker: always grouped under each Files Disk / USB stick name, clash suffix,
stable ids, Engine contract for .idea-usb.json, and the Nextcloud field toggle (headless)."""
import json
import os
import shutil

import pytest

from test_usb import UUID_A, UUID_B, kolibri_stick  # noqa: F401

OUT = os.environ.get("SCREENSHOT_DIR", os.path.join(os.path.dirname(__file__), "screenshots"))


def two_disks_two_sticks(s, fu, same_names=True):
    root = s.FILES_ROOT
    shutil.copytree(os.path.join(root, "disk1"), os.path.join(root, "grade-5a-0a1b2c"))
    os.rename(os.path.join(root, "disk1"), os.path.join(root, "grade-5a-9f8e7d"))
    with open(os.path.join(root, ".idea-files.json"), "w") as f:
        json.dump({"grade-5a-0a1b2c": "Grade 5A", "grade-5a-9f8e7d": "Grade 5A" if same_names else "Staff"}, f)
    tree = {"Science/Leaf.pdf": fu.samples / "Science/Reading/Worksheet.pdf"}
    fu.plug("sdb1", UUID_A, "MARY-STICK", tree)
    fu.plug("sdc1", UUID_B, "MARY-STICK" if same_names else "JOHN-STICK", tree)


def test_unique_labels(builder_env):
    s, _ = builder_env
    assert s.unique_labels(["A", "B", "A", "A", "A (2)"]) == ["A", "B", "A (2)", "A (3)", "A (2) (2)"]


def test_single_disk_and_stick_named(builder_env):
    s, fu = builder_env
    with open(os.path.join(s.FILES_ROOT, ".idea-files.json"), "w") as f:
        json.dump({"disk1": "Grade 5A"}, f)
    fu.plug("sdb1", UUID_A, "MARY-STICK", {"Science/Leaf.pdf": fu.samples / "Science/Reading/Worksheet.pdf"})
    page = s.render()
    assert '<option value="files" selected>Files Disk: Grade 5A</option>' in page
    assert '<option value="usb">USB: MARY-STICK</option>' in page
    assert '<optgroup label="Files Disk: Grade 5A">' in page and '<optgroup label="USB: MARY-STICK">' in page
    assert '>Files Disk: Grade 5A / Science</option>' in page and '>USB: MARY-STICK / Science</option>' in page
    for generic in ('>Files Disk<', '>USB stick<', '>USB<'):
        assert generic not in page


def test_disk_name_falls_back_to_folder(builder_env):
    s, _ = builder_env  # no .idea-files.json
    assert [g["label"] for g in s.picker_groups()] == ["Files Disk: disk1"]


def test_two_disks_two_sticks_grouped_with_clash_suffix(builder_env):
    s, fu = builder_env
    two_disks_two_sticks(s, fu)
    g = s.picker_groups()
    assert [(x["kind"], x["id"], x["label"]) for x in g] == [
        ("files", "grade-5a-0a1b2c", "Files Disk: Grade 5A"),
        ("files", "grade-5a-9f8e7d", "Files Disk: Grade 5A (2)"),
        ("usb", "usb-" + UUID_A, "USB: MARY-STICK"),
        ("usb", "usb-" + UUID_B, "USB: MARY-STICK (2)")]
    # values are the stable disk folder / stickId paths, never the display name
    assert ("grade-5a-9f8e7d/Science", "Science") in g[1]["options"]
    assert ("usb-%s/Science" % UUID_B, "Science") in g[3]["options"]
    page = s.render()
    assert '<option value="usb-%s/Science">USB: MARY-STICK (2) / Science</option>' % UUID_B in page
    # selection by id builds from the right stick
    title, _ = s.prepare_build(dict(source="usb", usb_folder="usb-%s/Science" % UUID_B, name="x", author="T"))
    assert title == "x (USB: MARY-STICK (2) / Science)"


def test_stick_missing_from_json_is_gone_and_extras_ignored(builder_env):
    s, fu = builder_env
    fu.plug("sdb1", UUID_A, "MARY-STICK", {"a/x.pdf": fu.samples / "Science/Reading/Worksheet.pdf"})
    fu.plug("sdc1", UUID_B, "JOHN-STICK", {"a/x.pdf": fu.samples / "Science/Reading/Worksheet.pdf"})
    # Engine wrapped shape with unknown extras; sdc1 folder still exists but is not listed
    (fu.root / ".idea-usb.json").write_text(json.dumps({"version": 1, "future": {"x": 1}, "sticks": [dict(
        stickId="usb-" + UUID_A, device="sdb1", label="MARY-STICK", displayName="USB: MARY-STICK",
        dir="sdb1", fsType="vfat", kolibriFolder=None, somethingNew=[1, 2])]}))
    assert [x["label"] for x in s.picker_groups() if x["kind"] == "usb"] == ["USB: MARY-STICK"]
    assert "JOHN-STICK" not in s.render()


# -- headless browser ------------------------------------------------------
pw = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def pwb():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # pragma: no cover
            pytest.skip("no headless chromium: %s" % e)
        yield b
        b.close()


def shot(page, name):
    os.makedirs(OUT, exist_ok=True)
    assert page.locator(".err:visible").count() == 0
    page.screenshot(path=os.path.join(OUT, name + ".png"), full_page=True)


def test_nextcloud_fields_toggle(builder_env, http, pwb, monkeypatch):
    s, fu = builder_env
    monkeypatch.setattr(s, "NEXTCLOUD_URL", "http://nc.invalid")
    kolibri_stick(fu)
    page = pwb.new_page(viewport={"width": 900, "height": 900})
    page.goto(http + "/")
    for src in ("files", "usb"):
        page.select_option("#source", src)
        assert not page.locator("#nc_user").is_visible() and not page.locator("#nc_password").is_visible()
    page.select_option("#source", "nextcloud")
    assert page.locator("#nc_user").is_visible() and page.locator("#nc_password").is_visible()
    assert not page.locator("#folder").is_visible()
    page.select_option("#source", "files")
    assert not page.locator("#nc_user").is_visible()
    # without JS every field stays usable, with a note
    ctx = pwb.new_context(java_script_enabled=False)
    p2 = ctx.new_page()
    p2.goto(http + "/")
    assert p2.locator("#nc_user").is_visible() and p2.locator("#usb_folder").is_visible()
    assert "JavaScript is off" in p2.inner_text("body")
    ctx.close()


def test_picker_screens(builder_env, http, pwb, monkeypatch):
    s, fu = builder_env
    monkeypatch.setattr(s, "LAN_IP", "192.168.1.20")
    with open(os.path.join(s.FILES_ROOT, ".idea-files.json"), "w") as f:
        json.dump({"disk1": "Grade 5A"}, f)
    fu.plug("sdb1", UUID_A, "MARY-STICK", {"Science/Leaf.pdf": fu.samples / "Science/Reading/Worksheet.pdf"})
    s.AUTO.poll()
    page = pwb.new_page(viewport={"width": 900, "height": 900})
    page.goto(http + "/")
    assert page.locator("#source option").all_inner_texts() == ["Files Disk: Grade 5A", "USB: MARY-STICK"]
    shot(page, "08-single-disk-single-stick")
    # phone width
    ph = pwb.new_page(viewport={"width": 375, "height": 812}, device_scale_factor=2)
    ph.goto(http + "/")
    assert ph.evaluate("document.documentElement.scrollWidth") <= 375
    shot(ph, "09-phone-width")
    # two disks + two sticks, same names -> grouped, (2) suffix
    fu.unplug("sdb1")
    os.remove(os.path.join(s.FILES_ROOT, ".idea-files.json"))
    two_disks_two_sticks(s, fu)
    s.AUTO.poll()
    page.goto(http + "/")
    assert page.locator("#source option").all_inner_texts() == [
        "Files Disk: Grade 5A, Files Disk: Grade 5A (2)", "USB: MARY-STICK, USB: MARY-STICK (2)"]
    labels = page.eval_on_selector_all("#folder optgroup, #usb_folder optgroup", "e => e.map(x => x.label)")
    assert labels == ["Files Disk: Grade 5A", "Files Disk: Grade 5A (2)", "USB: MARY-STICK", "USB: MARY-STICK (2)"]
    page.select_option("#source", "usb")
    # render the open picker as a list so the screenshot shows the grouping
    page.eval_on_selector("#usb_folder", "e => e.size = 8")
    page.select_option("#usb_folder", "usb-%s/Science" % UUID_B)
    shot(page, "10-two-disks-two-sticks-picker")
    page.select_option("#source", "files")
    page.eval_on_selector("#folder", "e => e.size = 12")
    shot(page, "11-two-disks-picker")

"""Headless-browser UI test: drives the real page and screenshots the main states.

Screenshots go to $SCREENSHOT_DIR (default tests/screenshots). Normal states must
show no error message; the deliberate error-handling shot goes to errors/.
"""
import os
import threading
import time

import pytest

pw = pytest.importorskip("playwright.sync_api")
from test_usb import UUID_A, kolibri_stick, wait_idle  # noqa: E402

OUT = os.environ.get("SCREENSHOT_DIR", os.path.join(os.path.dirname(__file__), "screenshots"))


@pytest.fixture
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # pragma: no cover
            pytest.skip("no headless chromium: %s" % e)
        yield b
        b.close()


def shot(page, name, errors_expected=False):
    d = os.path.join(OUT, "errors") if errors_expected else OUT
    os.makedirs(d, exist_ok=True)
    errs = page.locator(".err:visible").count()
    if errors_expected:
        assert errs > 0
    else:
        assert errs == 0, "%s shows an error: %s" % (name, page.locator(".err").all_inner_texts())
    page.screenshot(path=os.path.join(d, name + ".png"), full_page=True)


def test_ui_screens(builder_env, http, browser, monkeypatch, tmp_path):
    s, fu = builder_env
    monkeypatch.setenv("UI_POLL_MS", "200")
    monkeypatch.setattr(s, "LAN_IP", "192.168.1.20")
    files_root = s.FILES_ROOT
    page = browser.new_page(viewport={"width": 900, "height": 900})

    # 1. empty state: no Files Disk, no stick, no Nextcloud
    monkeypatch.setattr(s, "FILES_ROOT", str(tmp_path / "none"))
    page.goto(http + "/")
    assert page.locator("#empty").is_visible()
    shot(page, "01-empty-state")
    monkeypatch.setattr(s, "FILES_ROOT", files_root)

    # 2. Files Disk source: only its folder field is visible
    page.goto(http + "/")
    assert page.locator("#folder").is_visible() and not page.locator("#usb_folder").is_visible()
    assert "http://192.168.1.20:" in page.locator("#kolibri-address").inner_text()
    shot(page, "02-form-files-disk")

    # 3. a stick without a kolibri folder; USB source chosen -> only the USB field
    fu.plug("sdc1", "9999-0000", "JOHN-STICK", {"Science/Leaf.pdf": fu.samples / "Science/Reading/Worksheet.pdf"})
    s.AUTO.poll()
    page.goto(http + "/")
    page.select_option("#source", "usb")
    assert page.locator("#usb_folder").is_visible() and not page.locator("#folder").is_visible()
    shot(page, "03-form-usb-source")

    # 4. auto-build in progress (slowed down) + a teacher's manual build queued behind it
    real = s.usb.make_check

    def slow(root, stick):
        chk = real(root, stick)

        def check():
            time.sleep(1.2)
            chk()
        return check
    monkeypatch.setattr(s.usb, "make_check", slow)
    kolibri_stick(fu)
    s.AUTO.poll()
    page.goto(http + "/")
    page.select_option("#source", "files")
    page.select_option("#folder", "disk1/Music")
    page.fill("#name", "Songs")
    page.fill("#author", "Ms Achieng")
    page.click("#build")
    page.wait_for_selector("#job-box >> text=Queued")
    page.wait_for_selector("#usb-body >> text=/Building 1\\/2/")
    shot(page, "04-autobuild-progress-and-queued")

    # 5. a second teacher opening the page sees the queue
    p2 = browser.new_page(viewport={"width": 900, "height": 900})
    p2.goto(http + "/")
    assert p2.locator("#queue-body >> text=Songs").is_visible()
    shot(p2, "05-second-teacher-sees-queue")

    # 6. manual build progresses and finishes (background + polling)
    page.wait_for_selector("#job-box >> text=Built", timeout=60000)
    wait_idle(s)
    page.wait_for_selector("#usb-body >> text=you can pull the stick out")
    shot(page, "06-built-usb-and-manual")
    monkeypatch.setattr(s.usb, "make_check", real)

    # 7. deliberate error: no channel name -> message, typed values kept
    page.goto(http + "/")
    page.select_option("#folder", "disk1/Science")
    page.fill("#author", "Ms Achieng")
    page.fill("textarea[name=description]", "Term 2")
    page.eval_on_selector("#name", "e => e.removeAttribute('required')")
    page.click("#build")
    page.wait_for_selector(".err")
    assert page.input_value("#author") == "Ms Achieng" and page.input_value("#folder") == "disk1/Science"
    shot(page, "07-error-values-kept", errors_expected=True)

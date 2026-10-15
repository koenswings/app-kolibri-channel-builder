import json
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest


@pytest.fixture
def server(samples, tmp_path, monkeypatch):
    from channel_builder import server as s
    files = tmp_path / "files"
    files.mkdir()
    shutil.copytree(str(samples), str(files / "disk1"))
    (files / "disk2").symlink_to("/etc")  # symlinks escaping FILES_ROOT must be refused
    monkeypatch.setattr(s, "FILES_ROOT", str(files))
    monkeypatch.setattr(s, "EXPORT_DIR", str(tmp_path / "export"))
    monkeypatch.setattr(s, "NEXTCLOUD_URL", "")
    monkeypatch.setattr(s, "REQUIRE_LOGIN", False)
    srv = s.ThreadingHTTPServer(("127.0.0.1", 0), s.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()


def post(url, data):
    req = urllib.request.Request(url, urllib.parse.urlencode(data).encode(), headers={"Accept": "application/json"})
    return urllib.request.urlopen(req)


def test_ui_lists_folders(server):
    body = urllib.request.urlopen(server + "/").read().decode()
    assert "disk1/Science" in body and "All Rights Reserved" in body


def test_build_and_peer_endpoints(server):
    r = json.load(post(server + "/build", {"source": "files", "folder": "disk1/Science", "name": "Sci", "author": "T"}))
    cid = r["channel_id"]
    info = json.load(urllib.request.urlopen(server + "/api/public/info/?v=2"))
    assert info["application"] == "kolibri"
    chans = json.load(urllib.request.urlopen(server + "/api/public/v1/channels/lookup/" + cid))
    assert chans[0]["id"] == cid and chans[0]["total_resource_count"] == 5
    assert urllib.request.urlopen(server + "/content/databases/%s.sqlite3" % cid).status == 200


@pytest.mark.parametrize("folder", ["../../etc", "/etc", "disk1/../../", "disk2"])
def test_path_traversal_rejected(server, folder):
    with pytest.raises(urllib.error.HTTPError) as e:
        post(server + "/build", {"source": "files", "folder": folder, "name": "x", "author": "T"})
    assert e.value.code == 400


def test_storage_path_validation(server):
    for bad in ["/content/storage/../../etc/passwd", "/content/storage/a/b/" + "c" * 32 + ".pdf"]:
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(server + bad)


def test_nextcloud_required_when_login_enforced(server, monkeypatch):
    from channel_builder import server as s
    monkeypatch.setattr(s, "REQUIRE_LOGIN", True)
    with pytest.raises(urllib.error.HTTPError) as e:
        post(server + "/build", {"source": "files", "folder": "disk1", "name": "x", "author": "T"})
    assert e.value.code == 400

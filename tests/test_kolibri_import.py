"""Integration: built channel imports into a clean Kolibri (same version as app-kolibri)
via `importchannel/importcontent disk` and `... network` (builder as peer)."""
import os
import shutil
import subprocess
import sys
import threading

import pytest

KOLIBRI = shutil.which("kolibri") or os.path.join(os.path.dirname(sys.executable), "kolibri")
VERIFY = """
import sys, json
from kolibri.core.content.models import ChannelMetadata, ContentNode
cid = %r
res = ContentNode.objects.filter(channel_id=cid).exclude(kind='topic')
print('RESULT', json.dumps(dict(version=ChannelMetadata.objects.get(id=cid).version,
      total=res.count(), available=res.filter(available=True).count(),
      kinds=sorted(res.values_list('kind', flat=True)))))
"""


def kolibri(home, *args):
    env = dict(os.environ, KOLIBRI_HOME=str(home), KOLIBRI_RUN_MODE="test")
    p = subprocess.run([KOLIBRI, "manage"] + list(args), env=env, capture_output=True, text=True, timeout=600)
    assert p.returncode == 0, p.stdout[-3000:] + p.stderr[-3000:]
    return p.stdout + p.stderr


def verify(home, cid):
    out = kolibri(home, "shell", "-c", VERIFY % cid)
    import json
    return json.loads(out.split("RESULT ", 1)[1].splitlines()[0])


@pytest.fixture
def clean_kolibri(tmp_path):
    home = tmp_path / "kolibri-home"
    kolibri(home, "migrate")
    return home


def test_disk_import(samples, tmp_path, clean_kolibri):
    from channel_builder.build import Builder
    r = Builder(str(samples), str(tmp_path / "export"), "Disk", "Ms T", "it-disk").build()
    src = str(tmp_path / "export" / "KOLIBRI_DATA")
    kolibri(clean_kolibri, "importchannel", "disk", r["channel_id"], src)
    kolibri(clean_kolibri, "importcontent", "disk", r["channel_id"], src)
    v = verify(clean_kolibri, r["channel_id"])
    assert v["total"] == v["available"] == 6
    assert v["kinds"] == ["audio", "document", "document", "html5", "video", "video"]


def test_network_import_and_update(samples, tmp_path, clean_kolibri, monkeypatch):
    from channel_builder import server as s
    from channel_builder.build import Builder
    export = tmp_path / "export"
    monkeypatch.setattr(s, "EXPORT_DIR", str(export))
    srv = s.ThreadingHTTPServer(("127.0.0.1", 0), s.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d" % srv.server_address[1]
    try:
        r = Builder(str(samples), str(export), "Net", "Ms T", "it-net").build()
        cid = r["channel_id"]
        kolibri(clean_kolibri, "importchannel", "network", cid, "--baseurl", url)
        kolibri(clean_kolibri, "importcontent", "network", cid, "--baseurl", url)
        v = verify(clean_kolibri, cid)
        assert v["version"] == 1 and v["total"] == v["available"] == 6
        # teacher adds a file and rebuilds -> Kolibri picks up the new version
        shutil.copy(str(samples / "Science" / "Reading" / "Worksheet.pdf"), str(tmp_path / "w2.pdf"))
        src2 = tmp_path / "src2"
        shutil.copytree(str(samples), str(src2))
        shutil.copy(str(tmp_path / "w2.pdf"), str(src2 / "Extra.pdf"))
        Builder(str(src2), str(export), "Net", "Ms T", "it-net").build()
        kolibri(clean_kolibri, "importchannel", "network", cid, "--baseurl", url)
        kolibri(clean_kolibri, "importcontent", "network", cid, "--baseurl", url)
        v = verify(clean_kolibri, cid)
        assert v["version"] == 2 and v["total"] == v["available"] == 7
    finally:
        srv.shutdown()

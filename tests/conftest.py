import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "builder"))


@pytest.fixture(scope="session")
def samples(tmp_path_factory):
    d = tmp_path_factory.mktemp("samples")
    subprocess.check_call([os.path.join(HERE, "make_samples.sh"), str(d)])
    return d


@pytest.fixture(autouse=True)
def _kolibri_home(tmp_path_factory, monkeypatch):
    # kolibri import needs a writable home; keep it out of ~/.kolibri
    monkeypatch.setenv("KOLIBRI_HOME", str(tmp_path_factory.getbasetemp() / "kh-builder"))


class FakeUsb(object):
    """A fake USB_ROOT: stick folders + the Engine's .idea-usb.json (idea#193 §4)."""

    def __init__(self, root):
        self.root = root
        self.sticks = []
        root.mkdir(exist_ok=True)
        self._write()

    def _write(self):
        import json
        (self.root / ".idea-usb.json").write_text(json.dumps(self.sticks))

    def plug(self, device, uuid, label, tree=None):
        import shutil
        d = self.root / device
        if not d.exists():
            d.mkdir()
            for rel, src in (tree or {}).items():
                p = d / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(src, bytes):
                    p.write_bytes(src)
                else:
                    shutil.copyfile(str(src), str(p))
        self.sticks = [s for s in self.sticks if s["device"] != device] + [dict(
            stickId="usb-" + uuid, device=device, label=label, displayName="USB: " + label,
            fsType="exfat", readOnly=True)]
        self._write()
        return d

    def unplug(self, device, keep_files=False):
        import shutil
        self.sticks = [s for s in self.sticks if s["device"] != device]
        self._write()
        d = self.root / device
        if keep_files:
            d.rename(self.root / ("." + device + "-pulled"))
        else:
            shutil.rmtree(str(d), ignore_errors=True)

    def replug(self, device, uuid, label):
        hidden = self.root / ("." + device + "-pulled")
        if hidden.exists():
            hidden.rename(self.root / device)
        return self.plug(device, uuid, label)


@pytest.fixture
def builder_env(samples, tmp_path, monkeypatch):
    """server module pointed at temp roots, with a fresh queue and auto-builder."""
    import shutil
    from channel_builder import server as s
    from channel_builder.jobs import BuildQueue
    files = tmp_path / "files"
    files.mkdir()
    shutil.copytree(str(samples), str(files / "disk1"))
    fu = FakeUsb(tmp_path / "usb")
    monkeypatch.setattr(s, "FILES_ROOT", str(files))
    monkeypatch.setattr(s, "USB_ROOT", str(fu.root))
    monkeypatch.setattr(s, "EXPORT_DIR", str(tmp_path / "export"))
    monkeypatch.setattr(s, "WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(s, "NEXTCLOUD_URL", "")
    monkeypatch.setattr(s, "REQUIRE_LOGIN", False)
    monkeypatch.setattr(s, "AUTO_BUILD", True)
    q = BuildQueue(s.BUILD_LOCK)
    monkeypatch.setattr(s, "QUEUE", q)
    monkeypatch.setattr(s, "AUTO", s.AutoBuilder())
    fu.samples = samples
    return s, fu


@pytest.fixture
def http(builder_env):
    import threading
    s, fu = builder_env
    srv = s.ThreadingHTTPServer(("127.0.0.1", 0), s.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()

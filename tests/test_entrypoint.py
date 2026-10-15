"""The entrypoint fixes root-owned Engine-created bind dirs, then drops privileges."""
import os
import shutil
import subprocess

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
EP = os.path.join(ROOT, "builder", "entrypoint.sh")


def test_dockerfile_uses_entrypoint_not_user_builder():
    df = open(os.path.join(ROOT, "Dockerfile")).read()
    assert 'ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]' in df
    assert "\nUSER builder" not in df  # must start as root to chown
    assert os.access(EP, os.X_OK)


def test_entrypoint_non_root_just_execs(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("runs as root")
    env = dict(os.environ, EXPORT_DIR=str(tmp_path / "e"), WORK_DIR=str(tmp_path / "w"))
    out = subprocess.check_output([EP, "id", "-u"], env=env).decode().strip()
    assert out == str(os.geteuid())


def _sudo():
    if os.geteuid() == 0:
        return []
    if shutil.which("sudo") and subprocess.call(["sudo", "-n", "true"], stderr=subprocess.DEVNULL) == 0:
        return ["sudo", "-n"]
    pytest.skip("needs root or passwordless sudo")


def test_entrypoint_as_root_chowns_root_owned_dirs_and_drops(tmp_path):
    pre = _sudo()
    e, w = tmp_path / "data" / "export", tmp_path / "data" / "work"
    subprocess.check_call(pre + ["mkdir", "-p", str(e / "KOLIBRI_DATA")])  # root-owned, like Docker
    subprocess.check_call(pre + ["chown", "-R", "0:0", str(tmp_path / "data")])
    uid = os.geteuid() if os.geteuid() != 0 else 1000
    cmd = pre + ["env", "EXPORT_DIR=%s" % e, "WORK_DIR=%s" % w, "BUILDER_UID=%d" % uid,
                 "BUILDER_GID=%d" % uid, EP, "sh", "-c",
                 'id -u; touch "$EXPORT_DIR/KOLIBRI_DATA/x" "$WORK_DIR/y" && echo ok']
    out = subprocess.check_output(cmd).decode().split()
    assert out == [str(uid), "ok"]
    for p in (e, e / "KOLIBRI_DATA", w):
        assert os.stat(p).st_uid == uid
    subprocess.call(pre + ["rm", "-rf", str(tmp_path / "data")])

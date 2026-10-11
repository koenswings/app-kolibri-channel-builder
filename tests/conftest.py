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

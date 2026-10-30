import os, subprocess
S = os.path.join(os.path.dirname(__file__), '..', 'script', 'publish-tags.sh')
def run(existing, *tags, tmp=None):
    # fake existence check: a script that exits 0 only for refs listed in EXISTING
    import tempfile
    fake = os.path.join(tempfile.mkdtemp(), 'exists')
    with open(fake, 'w') as f:
        f.write('#!/bin/sh\ncase " $EXISTING " in *" $1 "*) exit 0;; *) exit 1;; esac\n')
    os.chmod(fake, 0o755)
    env = dict(os.environ, EXISTING=' '.join(existing), TAG_EXISTS_CMD=fake)
    r = subprocess.run(['bash', S, 'r', *tags], env=env, capture_output=True, text=True, check=True)
    return r.stdout.split()
def test_existing_release_tag_is_skipped():
    assert run(['r:1.0-0.15.5', 'r:1.0'], '1.0-0.15.5', '1.0') == []
def test_new_tag_is_pushed():
    assert run(['r:1.0-0.15.5'], '1.0-0.15.5', '1.1') == ['r:1.1']
def test_main_always_pushed():
    assert run(['r:main'], 'main') == ['r:main']

"""idea#403: release check against the previous release tag (agent-app-dev check-compose --base-ref).

A stateful image (MariaDB, MySQL, Postgres, Mongo, Redis) that moves to a new series needs an App
major. script/check-base-ref.sh picks the highest release tag that is an ancestor of HEAD and not
HEAD's own tag. A shallow clone or a clone with no release tags fails (CHECK_BASE_REF_FIRST_RELEASE=1
for a real first release); release tags with none an ancestor = first release, skipped with a note.
Set AAD_CHECK_COMPOSE to agent-app-dev script/check-compose.mjs to run the compare.
Run in a full clone with tags (CI: fetch-depth: 0); in a shallow or tagless clone the first test fails.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "script" / "check-base-ref.sh"
AAD = os.environ.get("AAD_CHECK_COMPOSE", "")
NO_TAGS = ("no release tags in this clone: git fetch --tags; "
           "for a real first release set CHECK_BASE_REF_FIRST_RELEASE=1")


def git(*a):
    return subprocess.run(["git", "-C", str(REPO), *a], capture_output=True, text=True)


IS_GIT = git("rev-parse", "--git-dir").returncode == 0


class ReleaseBaseRef(unittest.TestCase):
    @unittest.skipUnless(IS_GIT, "not a git checkout")
    def test_previous_release_tag_in_this_clone(self):
        r = subprocess.run([str(SCRIPT), "--print-tag"], capture_output=True, text=True)
        # A shallow or tagless clone fails here: fetch full history and tags.
        self.assertEqual(r.returncode, 0, r.stderr)
        tag = r.stdout.strip()
        if not tag:
            return  # first release
        self.assertRegex(tag, r"^[0-9]+\.[0-9]+(-[A-Za-z0-9._]+)?$")
        self.assertEqual(git("merge-base", "--is-ancestor", tag, "HEAD").returncode, 0)
        self.assertNotIn(tag, git("tag", "--points-at", "HEAD").stdout.split())

    @unittest.skipUnless(AAD and Path(AAD).exists(), "AAD_CHECK_COMPOSE not set")
    def test_check_compose_base_ref(self):
        r = subprocess.run([str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_temp_repo(self):
        env = {k: v for k, v in os.environ.items() if k != "CHECK_BASE_REF_FIRST_RELEASE"}
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as c:
            def run(cwd, *a):
                r = subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)

            def g(*a):
                run(d, *a)

            os.mkdir(os.path.join(d, "script"))
            shutil.copy2(SCRIPT, os.path.join(d, "script", "check-base-ref.sh"))

            def at(dir_, args=("--print-tag",), **extra):
                return subprocess.run([os.path.join(dir_, "script", "check-base-ref.sh"), *args],
                                      capture_output=True, text=True, env={**env, **extra})

            def pick():
                r = at(d)
                self.assertEqual(r.returncode, 0, r.stderr)
                return r.stdout.strip()

            def no_tags(r):
                self.assertEqual(r.returncode, 1)
                self.assertEqual(r.stderr.strip(), NO_TAGS)

            def commit(m):
                Path(d, "f").write_text(m)
                g("add", ".")
                g("commit", "-qm", m)

            g("init", "-q", "-b", "main")
            commit("a")
            no_tags(at(d))                                  # no tags at all: fail
            g("tag", "zimbabwe-2026-10-20")
            no_tags(at(d))                                  # no *release* tags: fail
            r = at(d, CHECK_BASE_REF_FIRST_RELEASE="1")
            self.assertEqual((r.returncode, r.stdout), (0, ""))
            r = at(d, (), CHECK_BASE_REF_FIRST_RELEASE="1")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("first release", r.stdout)
            g("tag", "1.0")
            self.assertEqual(pick(), "")                    # only HEAD's own: first release
            commit("b")
            for t in ("1.2-3.0", "1.0.1"):
                g("tag", t)
            commit("c")
            g("tag", "1.10-3.0")                            # HEAD's own
            self.assertEqual(pick(), "1.2-3.0")
            g("checkout", "-qb", "side", "HEAD~2")
            commit("s")
            g("tag", "9.0")                                 # not an ancestor of main
            g("checkout", "-q", "main")
            self.assertEqual(pick(), "1.2-3.0")
            commit("d")
            self.assertEqual(pick(), "1.10-3.0")            # numeric minor order
            notags, shallow = os.path.join(c, "notags"), os.path.join(c, "shallow")
            run(c, "clone", "-q", "--no-tags", f"file://{d}", notags)
            no_tags(at(notags))                             # full clone without tags must not pass
            run(c, "clone", "-q", "--depth", "1", f"file://{d}", shallow)
            r = at(shallow)
            self.assertEqual(r.returncode, 1)
            self.assertIn("shallow clone", r.stderr)


if __name__ == "__main__":
    unittest.main()

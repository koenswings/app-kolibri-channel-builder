"""idea#403: release check against the previous release tag (agent-app-dev check-compose --base-ref).

A stateful image (MariaDB, MySQL, Postgres, Mongo, Redis) that moves to a new series needs an App
major. script/check-base-ref.sh picks the highest release tag that is an ancestor of HEAD and not
HEAD's own tag (none = first release: skipped with a note). Set AAD_CHECK_COMPOSE to agent-app-dev
script/check-compose.mjs to run the compare. Needs tags and full history (CI: fetch-depth: 0).
"""
import os
import shutil
import tempfile
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "script" / "check-base-ref.sh"
AAD = os.environ.get("AAD_CHECK_COMPOSE", "")


def git(*a):
    return subprocess.run(["git", "-C", str(REPO), *a], capture_output=True, text=True)


IS_GIT = git("rev-parse", "--git-dir").returncode == 0


class ReleaseBaseRef(unittest.TestCase):
    @unittest.skipUnless(IS_GIT, "not a git checkout")
    def test_previous_release_tag(self):
        r = subprocess.run([str(SCRIPT), "--print-tag"], capture_output=True, text=True)
        if git("rev-parse", "--is-shallow-repository").stdout.strip() == "true":
            self.assertEqual(r.returncode, 1)
            self.assertIn("shallow clone", r.stderr)
            return
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

    def test_tag_choice_on_temp_repo(self):
        with tempfile.TemporaryDirectory() as d:
            def g(*a):
                r = subprocess.run(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)

            os.mkdir(os.path.join(d, "script"))
            shutil.copy2(SCRIPT, os.path.join(d, "script", "check-base-ref.sh"))

            def pick():
                return subprocess.run([os.path.join(d, "script", "check-base-ref.sh"), "--print-tag"],
                                      capture_output=True, text=True).stdout.strip()

            def commit(m):
                Path(d, "f").write_text(m)
                g("add", ".")
                g("commit", "-qm", m)

            g("init", "-q", "-b", "main")
            commit("a")
            self.assertEqual(pick(), "")            # no tags: first release
            g("tag", "1.0")
            self.assertEqual(pick(), "")            # only HEAD's own tag
            commit("b")
            for t in ("1.2-3.0", "zimbabwe-2026-10-20", "1.0.1"):
                g("tag", t)
            commit("c")
            g("tag", "1.10-3.0")                    # HEAD's own
            self.assertEqual(pick(), "1.2-3.0")
            g("checkout", "-qb", "side", "HEAD~2")
            commit("s")
            g("tag", "9.0")                         # not an ancestor of main
            g("checkout", "-q", "main")
            self.assertEqual(pick(), "1.2-3.0")
            commit("d")
            self.assertEqual(pick(), "1.10-3.0")    # numeric minor order


if __name__ == "__main__":
    unittest.main()

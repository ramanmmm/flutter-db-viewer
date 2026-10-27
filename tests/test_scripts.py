"""The per-project launcher (examples/checkLiveDb.sh) under a UTF-8 locale.

macOS ships bash 3.2, which treats a multibyte character right after `$VAR`
as part of the name — so these run the real script with stand-in `curl` and
`flutterdb` commands.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")

CURL = """#!/bin/sh
cat <<'EOS'
mkdir -p "$HOME/.local/bin"
printf '#!/bin/sh\\nif [ "$1" = --version ]; then echo "flutterdb %s"; else echo "ARGS:$*"; fi\\n' "${FLUTTERDB_REF#v}" > "$HOME/.local/bin/flutterdb"
chmod +x "$HOME/.local/bin/flutterdb"
EOS
"""


@unittest.skipUnless(BASH and os.name == "posix", "needs bash")
class LauncherScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home, self.stubs, self.proj = root / "home", root / "stubs", root / "proj"
        for d in (self.home, self.stubs, self.proj):
            d.mkdir()
        shutil.copy(ROOT / "examples" / "checkLiveDb.sh", self.proj / "checkLiveDb.sh")
        (self.stubs / "curl").write_text(CURL)
        (self.stubs / "curl").chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def installed(self, version):
        b = self.home / ".local" / "bin"
        b.mkdir(parents=True, exist_ok=True)
        f = b / "flutterdb"
        f.write_text(f'#!/bin/sh\nif [ "$1" = --version ]; then echo "flutterdb {version}"; else echo "ARGS:$*"; fi\n')
        f.chmod(0o755)

    def run_script(self, *args):
        env = {"HOME": str(self.home), "PATH": f"{self.stubs}:/usr/bin:/bin",
               "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "FLUTTERDB_REF": "v9.9.9"}
        p = subprocess.run([BASH, str(self.proj / "checkLiveDb.sh"), *args], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout + p.stderr

    def test_installs_when_missing(self):
        out = self.run_script("--once")
        self.assertIn("installing v9.9.9…", out)
        self.assertIn("ARGS:--once", out)

    def test_upgrades_an_older_install(self):
        self.installed("1.0.0")
        out = self.run_script()
        self.assertIn("Updating flutterdb to v9.9.9…", out)
        self.assertIn("ARGS:", out)

    def test_leaves_the_pinned_version_alone(self):
        self.installed("9.9.9")
        out = self.run_script("--once")
        self.assertNotIn("nstalling", out)
        self.assertNotIn("Updating", out)
        self.assertIn("ARGS:--once", out)

    def test_stop_passes_through(self):
        self.installed("9.9.9")
        self.assertIn("ARGS:stop", self.run_script("stop"))


if __name__ == "__main__":
    unittest.main()

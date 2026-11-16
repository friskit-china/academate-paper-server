"""Enter the selected instance's private environment from any host working directory."""

import json
import os
import sys
from pathlib import Path

if sys.argv[1:3] == ["amp", "uninstall"]:
    # The installed wrapper calls <prefix>/current/launcher.py. Keep the original
    # path to identify this prefix, and the host HOME/PATH to find its amp links.
    script = Path(__file__).with_name("uninstall.py")
    os.execv(sys.executable, [sys.executable, "-IB", str(script), "--amp", *sys.argv[3:]])

release = Path(__file__).resolve().parent
root = release.parent.parent
environment = {
    **json.loads((release / "environment.json").read_text()),
    **json.loads((root / "instance.json").read_text()),
}
if sys.argv[1] == "configure-https":
    # The operator's relative --cert/--key paths are relative to their shell.
    command = ["-I", str(release / "configure_https.py"), "--root", str(root), *sys.argv[2:]]
else:
    os.chdir(release / "backend")
    if sys.argv[1] == "setup-url":
        command = [
            "-m", "app.setup_cli", "prepare-container-setup",
            "--web-origin", environment["WEB_PUBLIC_ORIGIN"],
        ]
    else:
        command = ["-m", "app.amp", *sys.argv[2:]]
os.execve(sys.executable, [sys.executable, *command], environment)

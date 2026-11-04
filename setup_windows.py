"""Create and validate a local Windows environment using only the standard library."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


HEALTH_CHECK = (
    "import shimmer_live.__main__; import shimmer_live.export; "
    "import shimmer_live.sdlog; import shimmer_live.bt.windows; "
    "import shimmer_live.plugins.builtin.hrv"
)


def prepare(root: Path, *, compatibility=None, repair=False, verify=True):
    env = root / ".venv"
    python = env / "Scripts" / "python.exe"
    marker = env / "shimmer-setup.json"
    try:
        previous = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(previous, dict):
            previous = {}
    except (OSError, ValueError):
        previous = {}
    if compatibility is None:
        compatibility = bool(previous.get("compatibility", False))
    files = [root / "requirements.txt"]
    if compatibility:
        files.append(root / "constraints-windows-compat.txt")
    fingerprint = hashlib.sha256(b"\0".join(p.read_bytes() for p in files)).hexdigest()
    expected = {"version": 1, "requirements_sha256": fingerprint,
                "compatibility": compatibility}
    install = repair or not python.exists() or previous != expected

    def run(*args):
        subprocess.run([str(python), *args], cwd=root, check=True)

    if install or verify:
        # Only a completed install AND health check may mark the environment ready.
        marker.unlink(missing_ok=True)
        if not python.exists():
            print("Creating this PC's private Python environment...", flush=True)
            subprocess.run([sys.executable, "-m", "venv", str(env)], cwd=root, check=True)
        run("-c", "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ required'")
        if install:
            print("Installing Shimmer Live dependencies...", flush=True)
            args = ["-m", "pip", "install", "-r", str(files[0])]
            if compatibility:
                args += ["-c", str(files[1])]
            run(*args)
        run("-m", "pip", "check")
        run("-c", HEALTH_CHECK)
        # Keep a diagnostic snapshot, not a machine-specific default requirement.
        result = subprocess.run([str(python), "-m", "pip", "freeze"], cwd=root,
                                check=True, capture_output=True, text=True)
        (env / "shimmer-installed.txt").write_text(result.stdout, encoding="utf-8")
        marker.write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
        print("Shimmer Live environment is ready.", flush=True)
    return python


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launch", action="store_true", help="start the app after setup")
    parser.add_argument("--repair", action="store_true", help="rerun dependency installation and checks")
    profile = parser.add_mutually_exclusive_group()
    profile.add_argument("--compat-pandas", dest="compatibility", action="store_const", const=True,
                         default=None, help="explicitly use the optional pandas 2.3.3 fallback")
    profile.add_argument("--standard", dest="compatibility", action="store_const", const=False,
                         help="remove the optional pandas constraint for future installs")
    args, app_args = parser.parse_known_args(argv)
    if app_args and not args.launch:
        parser.error("unrecognized arguments: " + " ".join(app_args))
    if sys.platform != "win32" or sys.version_info < (3, 11):
        parser.error("Use Windows with Python 3.11 or newer. On Linux use run_linux.sh.")
    root = Path(__file__).resolve().parent
    try:
        python = prepare(root, compatibility=args.compatibility, repair=args.repair,
                         verify=not args.launch)
        if args.launch:
            return subprocess.call([str(python), "-m", "shimmer_live", *app_args], cwd=root)
        return 0
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"\nSetup failed: {exc}", file=sys.stderr)
        print("Fix the error above, then rerun setup_windows.bat --repair.\n"
              "If Windows explicitly blocks a pandas DLL, see WINDOWS.md for the optional fallback.\n"
              "Do not disable Windows security checks.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

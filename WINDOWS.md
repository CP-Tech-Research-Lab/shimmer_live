# Windows installation

## Portable Windows release

Extract the complete Windows release ZIP and open `ShimmerLive.exe` inside the
`ShimmerLive` folder. Keep `_internal` beside the executable. Python and the app's
dependencies are bundled; users do not need Python, pip, or a virtual environment.

Both the GitHub release workflow and `build_windows.bat` apply
`constraints-windows-compat.txt` when installing build dependencies. This bundles
pandas 2.3.3 instead of the newer pandas binary blocked by Smart App Control in
the v0.3.1 release. Builds start with a clean PyInstaller cache and must produce
a successful executable self-test report before being considered ready. Build
scripts isolate `PATH` so unrelated installed tools cannot supply conflicting
DLLs to PyInstaller. The
release includes `build-packages.txt` with the resolved package versions.

This change requires a new build; it cannot repair an already downloaded v0.3.1
ZIP. A passing CI startup test does not establish Smart App Control acceptance
on another PC. Validate the finished ZIP on a protected Windows PC before
distribution. Trusted code signing of the executable and bundled native
components remains necessary for a dependable signed distribution; these builds
do not add signing or change Windows security settings.

## Running from source

Use a local virtual environment on each PC. It keeps the application's Python packages separate from other programs while preserving native access to Windows Bluetooth, COM ports, USB bases and the desktop.

## First installation

1. Install 64-bit Python 3.11 or newer from [python.org](https://www.python.org/downloads/windows/), including the Python launcher or **Add Python to PATH**. Python 3.12 was used for the physical-device tests.
2. Download or clone this repository into a writable local folder. Do not copy another PC's `.venv` folder.
3. Connect the PC's Wi-Fi/Bluetooth antenna, if it has external antenna connectors, even when using Ethernet. Enable Bluetooth and install the PC manufacturer's Bluetooth and dock drivers if Windows does not supply them.
4. Double-click `run_windows.bat`. The first run needs internet access to create `.venv`, install dependencies and check that they load. Subsequent runs reuse the environment. Changed requirements or an incomplete setup trigger installation again.

Python must already be installed; the launcher does not install system drivers or change Windows security policy. Each PC needs its own Bluetooth pairings. SDLog-only firmware cannot provide Bluetooth live data.

## Check or repair an installation

Run these commands from the repository folder:

```bat
setup_windows.bat
setup_windows.bat --repair
```

The first verifies the installed dependencies. The second reruns dependency installation as well. A failed setup returns an error and does not start the app. Details remain visible when using `run_windows.bat`.

The installer records the installed package versions in `.venv\shimmer-installed.txt` for diagnosis. It records successful setup in `.venv\shimmer-setup.json`. Neither file belongs in Git. Settings and recordings are separate from `.venv`; do not delete those when troubleshooting.

## Optional pandas compatibility fallback

The default requirements retain `pandas>=2.0`; they do not impose a version based on one PC. One test PC's Smart App Control blocked a pandas 3.1.0 DLL, while the official pandas 2.3.3 wheel loaded successfully. If your PC shows that specific problem, try:

```bat
setup_windows.bat --compat-pandas
```

This explicitly applies `constraints-windows-compat.txt` in this environment only. Successful setup remembers that choice for later launches and repairs. It does not disable security checks or guarantee that another PC's policy will accept the wheel. If setup fails, rerun the same command after resolving the reported error.

To stop applying the compatibility constraint:

```bat
setup_windows.bat --standard
```

This removes the constraint; it does not force an upgrade of already-satisfactory packages. Creating a fresh environment uses the current default requirements. Installing packages in a virtual environment does not exempt their DLLs from Windows security policy.

## Portability and limits

A virtual environment is [recreated on each PC, not copied](https://docs.python.org/3/library/venv.html). This setup isolates packages but uses version ranges, so fresh installations at different dates can resolve different versions. The CI workflow tests those requirements on Windows and Linux. The recorded package snapshot aids reproduction; it is not a universal lockfile.

Docker is not the distribution route for this desktop app: it needs native Windows Bluetooth APIs, a GUI and USB/COM access. [Docker Desktop has no direct USB passthrough](https://docs.docker.com/desktop/troubleshoot-and-support/faqs/general/); USB/IP forwarding adds setup requirements. It would not remove driver, antenna or firmware dependencies.

A future signed Windows installer that bundles Python and a tested dependency set could remove the separate Python-install step. That requires a release build, signing and validation on other PCs; this repository does not yet ship such an installer.

Hardware validation on Windows 11 / Intel AX201 covered eleven docked LogAndStream Shimmers, with up to four simultaneous streams. One sensor needed a docked power cycle after a reconnect failure. SD retrieval and export passed, but a complete software-controlled SD start/stop cycle remains unverified. These tests do not guarantee every adapter, firmware or Windows configuration.

# Shimmer Live

Desktop app (Linux and Windows) to pair Shimmer3 sensors, view their data in real time and record it to CSV.

## Run

- **Linux:** `./run_linux.sh`
- **Windows:** double-click `run_windows.bat`. It needs Python 3.11+ from python.org, with "Add to PATH" or the `py` launcher.

The first run creates `.venv` and installs `requirements.txt`. After that you can also start the app with `python -m shimmer_live` from inside the venv.

## Using it

1. **Devices page.** It lists the paired devices. Tick the ones you want to use.
   - *Test connection* reads each device's firmware, sampling rate, sensors and battery.
   - *Forget* removes a pairing.
   - *⚙ Settings* connects to the device and shows its firmware, battery and configuration. From there you can change the sampling rate (with an estimate of the Bluetooth load), enabled sensors, ECG/ExG gains, GSR range and device name, and set the device clock to the PC's time. Changes are written to the Shimmer and read back to verify them.
   - *Scan for devices* finds new Shimmers. *Pair* pairs one, retrying up to 3 times. If the device asks for a PIN, a pop-up appears; the PIN is **1234**. After pairing, the device is tested automatically.
   - The *Guide* tab explains how to connect a Shimmer, what the LEDs mean, and what to do when a device doesn't respond. The *Log* tab shows everything the app does.
2. **Show live data.** Opens one plot per signal, coloured per device, with a 5–60 s window. Signals above 128 Hz are averaged down to about 128 Hz for display only; recordings keep every sample. Each device has a status card showing state, measured vs. nominal sampling rate, battery and sample count, and a ⚙ button with device info.
3. **Alerts:**
   - Red banner: lost connection. The app reconnects automatically.
   - Red banner with a *Retry* button: the device could not connect.
   - Amber or red banner: low battery (below 20% / below 10%).
   - Amber banner: packet rate below 85% (weak link).
   - Amber banner: an ECG channel at full scale (lead off).
   - Amber banner: GSR with no skin contact.
   - Amber banner: low disk space.
   - While recording, a large flashing red panel shows *"LOST CONNECTION — device — hh:mm:ss and counting"*.
4. **Recording.** *Start recording* writes all channels of the connected devices to one CSV. *Stop recording* shows a summary: duration, samples received vs. expected, disconnections, time lost, longest gap. You can give the recording a name there.
   - Files go to `~/Documents/ShimmerRecordings`; change this with the *Folder…* button.
   - File name: `[name_]shimmer_YYYY-MM-DD_HH-MM-SS.mmm_UTC+hhmm.csv`. It always includes the exact start time.
   - Alongside it, a `.meta.json` file holds the device configuration, clock sources, connection events and the summary.
   - *ⓘ Timestamps* shows which system clock functions are used on the current machine, and how to synchronise other systems with the recording.

## CSV columns

| column | meaning |
|---|---|
| `device`, `mac` | which Shimmer the row comes from (one row per sample, all devices in one file) |
| `host_unix_ns`, `host_utc_iso` | system wall clock (UTC) when the packet was received: `time.time_ns()` |
| `shared_clock_ticks`, `shared_clock_s` | monotonic shared clock at reception, for syncing with other software on the same PC. On Windows: `QueryPerformanceCounter`, with seconds computed exactly like Unreal Engine's `FPlatformTime::Cycles64() * FPlatformTime::GetSecondsPerCycle64()`. On Linux: `clock_gettime(CLOCK_MONOTONIC)` in ns. |
| `shimmer_ts_raw` | the Shimmer's own 24-bit timestamp (32768 ticks/s, wraps every 512 s) |
| `shimmer_ticks`, `shimmer_time_s` | that timestamp unwrapped into a continuous count, and the same count in seconds |
| `<CH>_raw`, `<CH>_<unit>` | raw value and calibrated value: GSR in µS, ECG in mV (using the gain read from the device), battery in mV |

## Layout

```
shimmer_live/
  session.py      connect / stream / watchdog / auto-reconnect for one device
  transport.py    Bluetooth links (Linux RFCOMM socket, Windows COM port or socket)
  recorder.py     CSV writer and recording statistics
  clocks.py       system timestamps and clock description
  conversions.py  raw values to physical units
  device_config.py  read / change on-device settings
  bt/             pairing and discovery: bluez.py (Linux, D-Bus), windows.py (WinRT)
  ui/             Qt pages and dialogs
```

## Platform notes

- **Linux:** no root needed. The app opens RFCOMM sockets directly; `rfcomm bind` is not needed. The RFCOMM channel is looked up through SDP (`sdptool`). If that fails, the app tries channels in turn, and caches the result. Settings and logs are stored in `~/.config/shimmer_live/`.
- **Windows:** streaming goes through the outgoing Bluetooth COM port that Windows creates when you pair a Shimmer. Settings and logs are stored in `%APPDATA%\ShimmerLive\`. **The Windows code paths (pairing, COM port, shared clock) have not been run on a Windows machine yet.** The rest of the app (UI, sessions, recording) is shared code and has been tested on Linux.

## Tests

```
pip install -r requirements-dev.txt
python -m pytest
```

The tests cover the data path without Bluetooth: unit conversions, timestamp unwrapping across 24-bit wraps and reconnects, the CSV format and recording statistics, plot decimation, ECG register edits and the shared clock. They also import every module and build the UI pages off-screen.

GitHub Actions runs them on Linux and Windows (Python 3.11 and 3.14) on every push. The Windows job also runs the Windows shared-clock code and checks every WinRT call the Windows backend uses.

## License

GPL-3.0. See [LICENSE](LICENSE). The app uses [pyshimmer](https://github.com/seemoo-lab/pyshimmer), which is also GPL-3.0. You may use, modify and share it; if you distribute it, you must include the source code under the same license.

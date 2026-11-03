# Shimmer Live

Desktop app (Linux and Windows) to pair Shimmer3 sensors, view their data in real time, mark keypoints, run analysis plugins such as real-time HRV, and record everything to a single ZIP of Consensys-style CSV files with quality checks and notes.

## Run

- **Linux:** `./run_linux.sh`
- **Windows:** double-click `run_windows.bat`. It needs Python 3.11+ from python.org, with "Add to PATH" or the `py` launcher.

The first run creates `.venv` and installs `requirements.txt`. After that you can also start the app with `python -m shimmer_live` from inside the venv.

## Using it

1. **Start page (Devices).** Status pills at the top show whether Bluetooth is on and whether a Shimmer Base is connected. **Your Shimmers** lists every Shimmer once, whether it is paired with this computer, docked in the base, or both, with its base slot, pairing state and last test. Tick the ones to stream. Every button explains itself when you hover over it.
   - *Test connection* reads each Shimmer's firmware, sampling rate, sensors and battery.
   - *Forget pairing* removes this computer's pairing (docked Shimmers can be set up again with one click).
   - *⚙ Settings* connects to the device and shows its firmware, battery and configuration. From there you can change the sampling rate (a preset, or any value typed from 1 to 2048 Hz; the dialog shows the exact rate the device will use, since it divides a 32768 Hz clock by a whole number, e.g. 300 Hz → 32768 ÷ 109 = 300.62 Hz) with an estimate of the Bluetooth load, enabled sensors, ECG/ExG gains, GSR range and device name, and set the device clock to the PC's time. Changes are written to the Shimmer and read back to verify them.
   - **Shimmers in a base** (fastest way to set them up): when a Shimmer Base or Dock is connected by USB, the app notices it and shows each docked Shimmer in the list with its slot, read over USB (address and firmware). Tick the docked Shimmers you want and press *Set up docked Shimmers*: for each ticked one it pairs it by address (PIN 1234 entered automatically; you're asked only if it's refused), tests the connection and sets its clock, and shows "ready" or the problem in plain words. Logging-only firmware (SDLog) is flagged, since it can't stream. A pairing that exists but no longer works is replaced automatically. On a Base6U with 6 Shimmers: reading the slots takes ~15 s, setup ~7–10 s per Shimmer.
   - *Scan for Shimmers* finds Shimmers that aren't in a base. *Pair* pairs one, retrying up to 3 times; the PIN 1234 is entered automatically, and a pop-up asks you only if it is refused. After pairing, the device is tested automatically.
   - Tabs on the right:
     - *Guide:* how to connect a Shimmer, what the LEDs mean, and what to do when a device doesn't respond.
     - *Keypoints:* the 7 predefined keypoints (rest, experiment and task start/end, discard) plus your own, each with a colour.
     - *Log:* everything the app does.
   - *Plugins…* opens the plugin page (see below).
2. **Show live data.** One plot per signal, coloured per device, with a 5–60 s window.
   - Signals above 128 Hz are averaged to about 128 Hz for display only; recordings keep every sample.
   - Each device has a status card: state, received vs. nominal sampling rate, battery, plugin status, and a ⚙ button with device info.
   - **Values to record** (top left): *Raw device values* (default) or *Calibrated (mV, µS)*. See [Calibration](#calibration).
   - The **Signals panel** (left) has two buttons per signal, plugin outputs included:
     - **eye** shows or hides the signal in live view;
     - **dot** selects whether it is recorded. Record choices lock once a recording starts.
3. **Keypoints.** While recording, a button per keypoint (keys **1–9** for the first nine) marks the current moment. A line in the keypoint's colour appears on every plot, and the keypoint is saved with every Shimmer's data.
4. **Plugins.** Plugins ticked on the Plugins page run on every device that has the signals they need. Their outputs are plotted and can be recorded like any other signal. Built in: **HRV**, real-time R-peak detection on ECG giving heart rate, RR interval and RMSSD over the last 30 s, with artifact beats excluded. To write your own, see [PLUGINS.md](PLUGINS.md).
5. **Alerts:**
   - Red banner: lost connection. The app reconnects automatically.
   - Red banner with a *Retry* button: the device could not connect.
   - Amber or red banner: low battery (below 20% / below 10%).
   - Amber banner: packet rate below 85% (weak link).
   - Amber banner: an ECG channel at full scale (lead off).
   - Amber banner: GSR with no skin contact.
   - Amber banner: a plugin error.
   - Amber banner: low disk space.
   - While recording, a large flashing red panel shows *"LOST CONNECTION — device — hh:mm:ss and counting"*.
6. **Recording.**
   - *Stop recording* opens the save dialog. It shows a summary per device (samples received vs. expected, connection losses, time lost) and the **quality checks**, and has a **name** field and a **notes** box. *Save* writes the ZIP.
   - Closing the dialog never loses the recording. *Discard* deletes it after confirmation.
   - While recording, data goes to crash-safe working files. If the app closes during a recording, it offers to recover it at the next start.
   - Recordings go to `~/Documents/ShimmerRecordings`; change this with *Folder…*.
   - *ⓘ Timestamps* shows which system clock functions are used on this machine, and how other systems can sync with the recording.

## Recording format

`[name_]shimmer_YYYY-MM-DD_HH-MM-SS.mmm_UTC+hhmm.zip`. The file name always includes the exact start time.

| file | content |
|---|---|
| `<device>.csv` | one per Shimmer, in the Consensys "Calibrated" layout: a `"sep=,"` line, a row of column names, a row of units, then data |
| `<plugin> - <device>.csv` | plugin outputs, e.g. `HRV - Shimmer3-8395.csv` with heart rate, RR and RMSSD per beat |
| `keypoints.csv` | every keypoint with its time on every clock, including each Shimmer's own clock |
| `qc.csv` | the quality checks shown at save time |
| `info.json` | notes, app version, devices, plugins, connection losses/reconnects, clock sources |
| `notes.txt` | the notes, as plain text |

Every device and plugin file starts with three timestamps:

| column | unit | meaning |
|---|---|---|
| `<device>_Timestamp_Unix_CAL` | ms | sample time on the PC wall clock (UTC epoch), the same convention as Consensys |
| `<device>_Timestamp_Shared_CAL` | s | sample time on the shared clock. On Windows this is `QueryPerformanceCounter`, computed exactly like Unreal Engine's `FPlatformTime::Cycles64() * FPlatformTime::GetSecondsPerCycle64()`; on Linux, `CLOCK_MONOTONIC` |
| `<device>_Timestamp_CAL` | ms | Shimmer clock (32768 Hz crystal), 0 = first sample of the live session |

The two PC timestamps are *sampling* times, not packet arrival times. Bluetooth delays packets by tens to hundreds of milliseconds. For each connection period, the Shimmer clock is fitted to the PC clock along the lower edge of the arrival times (the lower convex hull); this removes the latency and corrects clock drift. In tests with simulated latency, this recovers sampling times to within about 1 ms.

The channels follow Consensys naming (`ECG_LL-RA_24BIT`, `ECG_LA-RA_24BIT`, `ECG_RESP_24BIT`, `ECG_Vx-RL_24BIT`, `GSR_Skin_Conductance`, `Battery`, …). By default they hold **raw device values** and end in `_RAW`. With *Calibrated* selected they hold physical units and end in `_CAL`, plus the derived `ECG_LL-LA_24BIT_CAL` as in Consensys. The last column, `<device>_Keypoint`, holds the keypoint name at the nearest sample.

## Calibration

Raw values are integers exactly as the Shimmer sent them; nothing is lost. The live plots, plugins and quality checks always use calibrated values. These are the nominal Shimmer3 conversions, the same ones Consensys and pyshimmer use for these sensors. Each recording's `info.json` also lists, per column, the formula and its parameters (e.g. the ECG gain read from the device), and the live view has an *ⓘ How values are calibrated* button.

| sensor | raw value | calibrated value |
|---|---|---|
| ECG/ExG (ADS1292R) | 24-bit signed integer | mV = raw × (2420 / gain) / (2²³ − 1). 2420 mV is the chip reference; gain is 1, 2, 3, 4, 6, 8 or 12, read from the chip at connect. ±8388607 = saturated (lead off) |
| GSR+ | 16-bit word: bits 15–14 = range, bits 11–0 = ADC | range selects Rf = 40.2 kΩ / 287 kΩ / 1 MΩ / 3.3 MΩ; V = ADC × 3.0 / 4095; R (kΩ) = Rf / (V / 0.5 − 1); conductance (µS) = 1000 / R. V ≤ 0.5 V means no skin contact (empty) |
| Battery | 12-bit ADC | mV = raw × 3000 / 4095 × 1.988 |
| ECG/EMG status, other channels | as sent | not calibrated |

The formulas live in one place in the code, `shimmer_live/conversions.py` (`CALIBRATION_DOC` and `calibrate_array`). A test checks that converting a recorded raw column gives exactly the values the live view shows.

The **quality checks** are heuristics with thresholds listed in `shimmer_live/qc.py`. They flag likely problems; they are not a clinical assessment:
- completeness and connection losses;
- ECG: saturation (lead off), flat signal, amplitude, 50/60 Hz mains interference;
- EDA: skin contact, implausible range, abrupt-change artifacts, flat signal;
- heart rate range (with the HRV plugin), battery, and unmatched start/end keypoints.

## Analysing recordings

[`examples/load_recording.py`](examples/load_recording.py) loads a recording ZIP with pandas:

```
python examples/load_recording.py recording.zip             # summary, notes, keypoints, QC problems
python examples/load_recording.py recording.zip --plot      # every channel, with keypoint lines
python examples/load_recording.py recording.zip --extract   # unzip next to the file
```

In your own code, `load_recording("recording.zip")` returns the devices and plugin outputs as DataFrames, plus the keypoints, QC, info and notes. It only needs pandas, so it can be copied on its own.

## Layout

```
shimmer_live/
  session.py        connect / stream / watchdog / auto-reconnect for one device
  transport.py      Bluetooth links (Linux RFCOMM socket, Windows COM port or socket)
  recorder.py       crash-safe working files while recording
  export.py         working files -> final ZIP (aligned timestamps, keypoints, QC, notes)
  alignment.py      Shimmer clock -> PC clock (lower convex hull per connection period)
  qc.py             quality checks
  keypoints.py      keypoint definitions
  plugins/          plugin interface (api.py), loader, host, built-in plugins (HRV)
  clocks.py         system timestamps and the shared clock
  conversions.py    raw values to physical units, Consensys channel names
  device_config.py  read / change on-device settings
  base.py           Shimmer Base6U/Base15U and Dock over USB: slots, addresses, firmware, clock
  dock_setup.py     one-click setup of docked Shimmers (pair, test, set clock)
  bt/               pairing and discovery: bluez.py (Linux, D-Bus), windows.py (WinRT)
  ui/               Qt pages and dialogs
examples/           recording loader, example plugin
```

## Platform notes

- **Linux:** no root needed. The app opens RFCOMM sockets directly; `rfcomm bind` is not needed. The RFCOMM channel is looked up through SDP (`sdptool`). If that fails, the app tries channels in turn, and caches the result. Settings, logs and user plugins are stored in `~/.config/shimmer_live/`.
- **Windows:** streaming goes through the outgoing Bluetooth COM port that Windows creates when you pair a Shimmer. Settings, logs and user plugins are stored in `%APPDATA%\ShimmerLive\`. **The Windows code paths (pairing, COM port, shared clock) have not been run on a Windows machine yet.** The rest of the app is shared code and has been tested on Linux.

## Tests

```
pip install -r requirements-dev.txt
python -m pytest
```

The tests run without Bluetooth. They cover:
- unit conversions;
- timestamp unwrapping across 24-bit wraps and reconnects;
- the recording pipeline end to end: timestamp alignment against known sampling times with simulated Bluetooth latency, keypoints, selected channels, the ZIP layout, notes and crash recovery;
- the quality checks;
- the HRV plugin against synthetic ECG with known beat times;
- plugin loading and isolation of plugin errors;
- plot decimation, ECG register edits and the shared clock.

They also import every module and build the UI pages off-screen.

GitHub Actions runs them on Linux and Windows (Python 3.11 and 3.14) on every push. The Windows job also runs the Windows shared-clock code and checks every WinRT call the Windows backend uses.

## License

GPL-3.0. See [LICENSE](LICENSE). The app uses [pyshimmer](https://github.com/seemoo-lab/pyshimmer), which is also GPL-3.0. You may use, modify and share it; if you distribute it, you must include the source code under the same license.

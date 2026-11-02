# Writing Shimmer Live plugins

A plugin turns live signals from one Shimmer into derived signals, such as heart rate from ECG or tonic level from EDA. Plugin outputs are plotted in live view next to the device's own signals. They can be shown or hidden (eye) and recorded or not (dot) like any other signal, and they are saved in the recording ZIP as `<plugin> - <device>.csv` with the same three timestamps as the device data.

The built-in **HRV** plugin (`shimmer_live/plugins/builtin/hrv.py`) and the example **EDA tonic** plugin (`examples/plugins/eda_tonic.py`) are complete, working references.

## Installing a plugin

1. Open the **Plugins** page (button on the start page), then **Open plugins folder**.
2. Put your `.py` file there. Each file can define one or more plugins.
3. Press **Reload**, tick the plugin, and open live data.

A file that fails to load is listed in red on the Plugins page with the error; it never stops the app.

## The interface

```python
from shimmer_live.plugins.api import InputSpec, OutputSpec, Plugin

class MyPlugin(Plugin):
    id = "my_plugin"            # unique, stable: used in settings and in recorded column names
    name = "My plugin"          # shown in the UI
    description = "What it computes."
    version = "1.0"
    inputs = [InputSpec(kind="exg", min_rate=100, preferred=("EXG1_CH1",))]
    outputs = [OutputSpec("hr", "Heart rate", "bpm", style="points")]

    def start(self, inputs): ...            # once, before the first process()
    def process(self, t, x): ...            # every ~100 ms with the new samples
    def reset(self): ...                    # optional: after a connection loss
    def stop(self): ...                     # optional: live view closing
```

### Inputs: `InputSpec`

| field | meaning |
|---|---|
| `kind` | `"exg"` (ECG/EMG), `"gsr"` (EDA), `"vbatt"` (battery) or `"other"` (any raw channel) |
| `min_rate` | minimum sampling rate in Hz |
| `preferred` | channel keys to try first. ECG: `EXG1_CH1` (LL-RA), `EXG1_CH2` (LA-RA), `EXG2_CH1` (RESP), `EXG2_CH2` (Vx-RL); EDA: `GSR` |
| `description` | shown on the Plugins page |

The app runs one instance of the plugin **per device** that can provide every input. If a device can't, its card in live view says why, e.g. "HRV: n/a (needs an ECG/EMG signal at ≥ 100 Hz)". To choose inputs differently, override the classmethod `match(signals)`; see `api.py`.

### Outputs: `OutputSpec`

| field | meaning |
|---|---|
| `key` | short identifier, used in `process()` results and in recorded column names |
| `label`, `unit` | shown in the UI and the units row of the CSV |
| `style` | `"line"` for regularly sampled outputs, `"points"` for one value per event (beat, peak, …) |

### `start(inputs)`

`inputs` is a list of `SignalInfo`, one per `InputSpec` in the same order. Each has `device`, `mac`, `key`, `label`, `unit`, `rate` (Hz) and `kind`. Set up your state here, e.g. design filters for `inputs[0].rate`.

### `process(t, x)` → `{output key: (times, values)}`

- `t`: numpy array `(n,)`, sample times in **seconds on the Shimmer clock**. Sample spacing is exact.
- `x`: numpy array `(n, number of inputs)`, **calibrated** values (mV, µS, …), columns in `inputs` order.
- Return only what was produced in this call. An empty dict is fine.
- Output times use the same Shimmer-clock seconds as `t`. They may lag the input (HRV reports a beat about 0.3 s after it happened) but must not be in the future.

Called on a dedicated worker thread, never the UI or Bluetooth thread, so plain numpy/scipy code is fine. Keep it fast: it runs about 10 times per second for every device.

### `reset()` and `stop()`

`reset()` is called after a connection loss: the next `t` jumps forward over the gap. The default restarts the plugin with `start()`. `stop()` is called when the live view closes.

### Errors

If `start`, `process` or `reset` raises, that plugin instance stops and a warning banner shows the error. The rest of the app, including other plugins and the recording, keeps running.

## Testing a plugin without a Shimmer

`process()` is plain numpy in, dict out, so you can feed it synthetic or recorded data:

```python
import numpy as np
from shimmer_live.plugins.api import SignalInfo
from eda_tonic import EDATonic

p = EDATonic()
p.start([SignalInfo("dev", "00:00:00:00:00:00", "GSR", "GSR", "uS", 32.0, "gsr")])
t = np.arange(320) / 32
print(p.process(t, (2 + 0.1 * np.sin(t))[:, None]))
```

`tests/test_plugins.py` tests the HRV plugin this way against synthetic ECG with known beat times.

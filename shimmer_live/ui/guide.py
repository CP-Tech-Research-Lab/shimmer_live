"""In-app guide: connecting new Shimmers, LED meanings, troubleshooting.

LED patterns are from the Shimmer User Manual rev3p (LogAndStream firmware, figs 2-2/2-3)."""


def _dot(*colors):
    return "".join(f'<span style="color:{c}; font-size:14pt;">●</span>' for c in colors)


B, G, Y, R = "#1e6fd9", "#1b9e3e", "#e0a100", "#d32f2f"

GUIDE_HTML = f"""
<h3>Your Shimmers: four groups</h3>
<p>Every Shimmer appears once, in the group that says what you can do with it right now. Nothing needs a
scan button: the page finds them by itself.</p>
<ul>
<li><b style="color:#1b7a3a">Ready</b> &mdash; paired with this computer, <b>not in a base</b> and switched on nearby.
    Only these can be ticked for <b>Show live data</b> and <b>Record on SD cards</b>.</li>
<li><b style="color:#1f4e8c">In the base</b> &mdash; read over USB. Tick them for <b>Set up docked Shimmers</b>
    (pairing, clock), and update their firmware. They are <b>not</b> used for live data or SD recording
    (PPG and SD recording do not work in the base): take them out, switched on, and they move to <b>Ready</b>.</li>
<li><b style="color:#9a6b00">Found nearby, not paired</b> &mdash; a Shimmer switched on near this computer that
    is not paired yet: press <b>Pair</b> in its row.</li>
<li><b style="color:#888">Paired, not found</b> (grey) &mdash; switched off, out of range, or connected to another
    program. Switch it on (blue blink every 2 s) and bring it closer: it moves to <b>Ready</b> at its next
    check.</li>
</ul>
<p><b>How the page finds them:</b> while it is open, it knocks on each paired Shimmer that is not in a base,
one after the other (a tiny Bluetooth connection request, nothing starts on the Shimmer), and every ~45 s it
looks briefly for Shimmers that are not paired yet. A switched-on Shimmer answers in about a second; a
switched-off one takes ~5 s to give up on, so with many switched off it can take up to a minute to reach a
given one. Ticked Shimmers are checked more often. The line above the table shows what is being checked right now; in the Where column <i>checking…</i> marks that Shimmer and <i>waiting…</i> one not checked yet.</p>
<p>Your ticks are remembered: a Shimmer you ticked earlier is ticked again as soon as it is in <b>Ready</b>.</p>
<p><b>Labels (e.g. the participant):</b> double-click a Shimmer's name (or use its ⚙ Settings) to give it a
label, e.g. <i>alberto</i>. It appears in grey next to the name &mdash; <b>Shimmer3-A281</b> <span
style="color:#8a8a86">(alberto)</span> &mdash; here, in live view, in the SD recording page and in every
recording (<i>devices.csv</i> and <i>info.json</i> in the ZIP). It stays on this computer: the Shimmer and its
Bluetooth name are not changed. A recording keeps the label it had when it started.</p>
<p><b>Battery, sensors, rate:</b> the app reads them by itself from every Shimmer in <b>Ready</b> as soon as it
gets there and then every 5 minutes (one Shimmer at a time; the Status column says <i>reading battery and
sensors…</i>). If a reading fails, the last values stay and Status shows why.</p>

<h3>Setting up Shimmers from the base (fastest)</h3>
<ol>
<li>Put the Shimmers in a <b>Shimmer Base</b> (or Dock) connected to this computer by USB and switch them on.</li>
<li>They appear under <b>In the base</b> with their slot (<i>base slot 1, 2, …</i>).</li>
<li>Tick them and press <b>Set up docked Shimmers</b>. For each ticked one the app pairs it (entering the PIN
    1234 for you), tests the connection and sets its clock, then shows <i>ready</i> or what is wrong in plain
    words. Shimmers with logging-only firmware (SDLog) are flagged: they cannot stream over Bluetooth.</li>
<li>Take them out of the base, switched on: they move to <b>Ready</b>, ticked. Press <b>Show live data</b>
    or <b>Record on SD cards</b>.</li>
</ol>
<p>If a paired Shimmer does not connect, setup keeps its pairing: a timeout can mean the radio is
temporarily busy or unreachable: it is checked again automatically once it is in <b>Ready</b>. If you need to replace a pairing,
use <b>Forget pairing</b>, then set it up again.</p>

<h3>Connecting a new Shimmer without a base</h3>
<ol>
<li><b>Charge and switch on.</b> Use the slide switch on the Shimmer.</li>
<li><b>Check the upper LED:</b> a short <b>blue blink every 2 s</b> {_dot(B)} means it is on and waiting for a connection.
    A <b>solid blue</b> LED means it is <i>already connected</i> to another program or computer &mdash; close that first
    (a Shimmer accepts only one Bluetooth connection).</li>
<li><b>Bring it close</b> (1&ndash;2 m, line of sight) for pairing.</li>
<li>Within about a minute it appears under <b>Found nearby, not paired</b> as <b>Shimmer3-XXXX</b>, where XXXX are
    the last 4 characters of the Bluetooth address printed on the Shimmer label. <b>Refresh</b> looks again at once.</li>
<li>Press <b>Pair</b> in its row. If a PIN is requested, the PIN is <b>1234</b>. Newer radios pair without a PIN.</li>
<li>After pairing the app reads its firmware, sampling rate, sensors and battery, and it moves to
    <b>Ready</b>, ticked.</li>
</ol>

<h3>If a Shimmer does not respond</h3>
<ul>
<li><b>Desktop PC antenna:</b> connect the external Wi-Fi/Bluetooth antenna to the PC's rear antenna
    sockets, even if you use Ethernet. Missing antennas can cause intermittent pairing and lost samples
    with nearby Shimmers. Position the antenna clear of the metal case.</li>
<li><b>Try again</b> &mdash; a Shimmer only listens for connections ~12.5% of the time (320 ms out of every 2.56 s),
    so several attempts are normal. The app retries automatically.</li>
<li>Make sure it is <b>not connected elsewhere</b> (Consensys, a phone, another PC, another copy of this app).</li>
<li><b>Battery</b>: a flat battery causes erratic behaviour &mdash; put it in the dock/base.</li>
<li><b>Reset</b>: switch it off and on (or the reset button on the dock), wait for the blue standby blink, retry.</li>
<li>If it was paired to another computer since, its saved pairing may need replacing:
    use <b>Forget pairing</b>, then pair again. A timeout alone does not prove this.</li>
<li>It must run Bluetooth firmware (<b>LogAndStream</b>); program it with Consensys if needed.</li>
<li><b>Windows</b>: each paired Shimmer gets an outgoing COM port (Settings &rarr; Bluetooth &rarr; More Bluetooth
    settings &rarr; COM Ports). The app finds it automatically.</li>
</ul>

<h3>LED meanings (LogAndStream firmware)</h3>
<p><b>Upper LEDs &mdash; connection / operation</b></p>
<table cellspacing="0" cellpadding="3">
<tr><td>{_dot(B)}</td><td>Blue 0.1 s on / 2 s off</td><td><b>Standby</b> &ndash; on, waiting for connection</td></tr>
<tr><td>{_dot(B)}</td><td>Blue solid</td><td><b>Bluetooth connected</b></td></tr>
<tr><td>{_dot(B)}</td><td>Blue 1 s on / 1 s off</td><td><b>Streaming</b></td></tr>
<tr><td>{_dot(G)}</td><td>Green 1 s on / 1 s off</td><td>Logging to SD only</td></tr>
<tr><td>{_dot(B, G)}</td><td>1 s blue / 1 s green</td><td>Streaming and logging</td></tr>
<tr><td>{_dot(B, G)}</td><td>0.1 s blue / 0.1 s green</td><td>RTC not set (harmless for streaming)</td></tr>
<tr><td>{_dot(G)}</td><td>Green 0.1 s on / 0.1 s off</td><td>Configuring (undocked)</td></tr>
<tr><td>{_dot(R, Y)}</td><td>0.1 s red / 0.1 s yellow</td><td>SD card error</td></tr>
</table>
<p><b>Lower LEDs &mdash; battery</b></p>
<table cellspacing="0" cellpadding="3">
<tr><td>{_dot(G)}</td><td>Green solid (docked)</td><td>Fully charged</td></tr>
<tr><td>{_dot(Y)}</td><td>Yellow solid (docked)</td><td>Charging</td></tr>
<tr><td>{_dot(G)}</td><td>Green 0.1 s on / 5 s off</td><td>Full charge (undocked)</td></tr>
<tr><td>{_dot(Y)}</td><td>Yellow 0.1 s on / 5 s off</td><td>Medium charge</td></tr>
<tr><td>{_dot(R)}</td><td>Red 0.1 s on / 5 s off</td><td><b>Low charge &ndash; recharge soon</b></td></tr>
</table>

<h3>More than 7 Shimmers: record on their SD cards</h3>
<ol>
<li>Tick the Shimmers, take them <b>out of the base</b> (switched on) and press <b>Record on SD cards…</b>.</li>
<li>Each one records on its own card. Mark keypoints on the SD recording page (keys 1&ndash;9). You can close the
    app meanwhile.</li>
<li>Press <b>Stop recording</b> <i>before</i> docking them: a Shimmer docked while recording loses its file.</li>
<li>Dock them and press <b>Import from base</b>: you get the usual recording ZIP.</li>
</ol>

<h3>During live view and recording</h3>
<ul>
<li>A <b>red banner</b> appears when a Shimmer stops sending data; the app reconnects on its own. Bring the device
    closer / check it is on to help it reconnect quickly.</li>
<li>While recording, a <b>large red panel</b> counts how long a device has been lost, so you can decide whether to
    stop the experiment. Data from the other devices keeps being recorded.</li>
<li>Battery warnings appear below 20% and become critical below 10%.</li>
<li><b>Signals panel</b> (left): the eye shows or hides a signal in the plots; the dot chooses whether it is recorded.
    Decide what to record <i>before</i> starting: the dots lock while recording.</li>
<li><b>Keypoints</b> (defined in the Keypoints tab): while recording, press a keypoint button or its number key
    (1&ndash;9). A coloured line appears on every plot and the mark is saved with every Shimmer's data.</li>
<li><b>Stop recording</b> shows the quality checks and lets you add a name and notes before saving. The recording is
    one .zip file with one CSV per Shimmer (Consensys layout), keypoints, quality checks and notes.</li>
</ul>
"""

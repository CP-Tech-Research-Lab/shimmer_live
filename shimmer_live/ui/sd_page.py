"""SD-card recording: the Shimmers record by themselves; this page starts, checks and stops them,
marks keypoints (saved at once, with this PC's clock) and imports the files through the base.

The session is kept in the settings, so closing the app (or this page) does not affect it."""
import datetime as dt
import logging

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from .. import labels
from .. import clocks
from .. import keypoints as kpmod
from .. import sdlog
from ..recorder import VALUES_RAW
from .branding import BrandHeader
from .common import (CRITICAL, INFO, FONT_L, FONT_S, GOOD, MUTED, WARNING, Card, FlowLayout, divider,
                     fmt_duration, run_bg, set_role, style_table, ui_call)
from .dialogs import SaveRecordingDialog

log = logging.getLogger("shimmerlive.ui.sd")

TIPS = {
    "back": "Back to the list of Shimmers. The SD recording keeps going.",
    "check": "Connect briefly to each Shimmer and confirm it is still recording (also measures its clock).",
    "stop": "Stop the recording on every Shimmer, one after the other. Do this BEFORE putting them in the base: "
            "a Shimmer docked while recording loses its file.",
    "import": "With the Shimmers back in the base: copy each one's files through the base and save them as a "
              "recording (same ZIP as live recordings). The files stay on the SD cards.",
    "discard": "Forget this SD session in the app. Nothing is deleted from the SD cards.",
    "retry": "Try again on the Shimmers that did not start.",
}


class StartSdDialog(QDialog):
    """Name the session and explain what happens before anything is sent to the Shimmers."""

    def __init__(self, parent, names):
        super().__init__(parent)
        self.setWindowTitle("Record on SD cards")
        lay = QVBoxLayout(self)
        intro = QLabel(
            f"<b>{len(names)} Shimmer(s):</b> {', '.join(names)}<br><br>"
            "Each Shimmer records on its own SD card, without Bluetooth, so any number can record at once. "
            "This app connects to each one briefly to set its clock to this PC's time, write the session name "
            "and start it.<br><br>"
            "<b>Before you start:</b> take the Shimmers <b>out of the base</b> and keep them switched on "
            "(a docked Shimmer does not record).<br>"
            "<b>To finish:</b> press <i>Stop recording</i> in this app <b>before</b> docking them; then dock them "
            "and press <i>Import from base</i>.<br><br>"
            "Keypoints can be marked in the app while they record. You can close the app meanwhile: "
            "the session is remembered.")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)
        intro.setMinimumWidth(560)
        lay.addWidget(intro)
        row = QHBoxLayout()
        row.addWidget(QLabel("Session name:"))
        self.name = QLineEdit(dt.datetime.now().strftime("SD%m%d_%H%M"))
        self.name.setMaxLength(sdlog.NAME_MAX)
        self.name.setToolTip(f"Up to {sdlog.NAME_MAX} letters, digits, _ or -. It names the folder on the SD cards.")
        row.addWidget(self.name, 1)
        lay.addLayout(row)
        self.hint = QLabel()
        self.hint.setStyleSheet(f"color: {MUTED}; font-size: {FONT_S}pt;")
        lay.addWidget(self.hint)
        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.ok = bb.addButton("Start recording", QDialogButtonBox.AcceptRole)
        set_role(self.ok, "record")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.name.textChanged.connect(self._check)
        self._check()

    def _check(self):
        clean = sdlog.clean_name(self.name.text())
        self.ok.setEnabled(bool(clean))
        self.hint.setText(f"Folder on the cards: data/{clean}_<start time>/" if clean else "Enter a name.")

    def session_name(self) -> str:
        return sdlog.clean_name(self.name.text())


class SdPage(QWidget):
    back = Signal()

    def __init__(self, cfg, base_watcher):
        super().__init__()
        self.cfg, self.base = cfg, base_watcher
        self.busy = ""                 # what is running in the background, "" when idle
        self.progress = {}             # mac -> progress text
        self._shortcuts = []

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        head = QHBoxLayout()
        self.btn_back = QPushButton("←  Back to Shimmers")
        self.btn_back.setToolTip(TIPS["back"])
        self.btn_back.clicked.connect(self.back.emit)
        head.addWidget(self.btn_back, 0, Qt.AlignVCenter)
        head.addSpacing(12)
        head.addWidget(BrandHeader("SD recording", compact=True))
        head.addStretch(1)
        root.addLayout(head)
        root.addWidget(divider())

        # ---- session status and actions ----
        card = Card("Recording on SD cards", "The Shimmers record by themselves. This app only starts, checks "
                                             "and stops them, and keeps your keypoints.")
        self.headline = QLabel()
        self.headline.setStyleSheet(f"font-size: {FONT_L}pt; font-weight: 700;")
        self.headline.setTextFormat(Qt.RichText)
        card.body.addWidget(self.headline)
        self.advice = QLabel()
        self.advice.setWordWrap(True)
        self.advice.setTextFormat(Qt.RichText)
        card.body.addWidget(self.advice)
        acts = QHBoxLayout()
        self.btn_retry = self._button("Retry failed", TIPS["retry"], self._retry)
        self.btn_check = self._button("Check status", TIPS["check"], self._check_all)
        self.btn_stop = self._button("■  Stop recording", TIPS["stop"], self._stop_all, "stop")
        self.btn_import = self._button("Import from base", TIPS["import"], self._import, "primary")
        self.btn_discard = self._button("Forget session…", TIPS["discard"], self._discard)
        for b in (self.btn_retry, self.btn_check, self.btn_stop, self.btn_import):
            acts.addWidget(b)
        acts.addStretch(1)
        acts.addWidget(self.btn_discard)
        card.body.addLayout(acts)
        cols = ["Shimmer", "State", "Clock vs PC", "Details"]
        self.table = QTableWidget(0, len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        style_table(self.table)
        hh = self.table.horizontalHeader()
        for i, mode in enumerate((QHeaderView.ResizeToContents,) * 3 + (QHeaderView.Stretch,)):
            hh.setSectionResizeMode(i, mode)
        self.table.horizontalHeaderItem(2).setToolTip(
            "Shimmer clock minus PC clock, measured over Bluetooth (best of several round trips). The offsets "
            "at start and stop are removed from the data at import.")
        card.body.addWidget(self.table, 1)
        root.addWidget(card, 1)

        # ---- keypoints ----
        kcard = Card("Keypoints", "Press a button (or its number key 1–9) to mark this moment. Saved at once "
                                  "with this PC's time and placed in every Shimmer's data at import.")
        self.kp_bar = FlowLayout(spacing=8)
        kcard.body.addLayout(self.kp_bar)
        self.kp_list = QListWidget()
        self.kp_list.setToolTip("Keypoints marked in this session")
        self.kp_list.setMaximumHeight(140)
        kcard.body.addWidget(self.kp_list)
        root.addWidget(kcard, 0)

        self.timer = QTimer(self, interval=1000, timeout=self._render)

    def _button(self, text, tip, slot, role=None):
        b = QPushButton(text)
        b.setToolTip(tip)
        b.clicked.connect(slot)
        if role:
            set_role(b, role)
        return b

    # ---------------- session ----------------
    @property
    def session(self):
        return sdlog.active(self.cfg)

    def start_new(self, name, devices):
        """devices: [(mac, name)]; called after StartSdDialog."""
        values = self.cfg.data.get("record_values", VALUES_RAW)
        defs = [{"name": k.name, "color": k.color} for k in kpmod.load(self.cfg)]
        sdlog.new_session(self.cfg, name, devices, values, defs)
        log.info("SD session %s: starting %d Shimmers", name, len(devices))
        self.open()
        self._start([m for m, _ in devices])

    def open(self):
        self._build_keypoints()
        self.kp_list.clear()
        for ev in (self.session or {}).get("keypoints", []):
            self._list_keypoint(ev)
        self.timer.start()
        self._render()

    def hideEvent(self, ev):
        self.timer.stop()
        super().hideEvent(ev)

    def showEvent(self, ev):
        self.timer.start()
        super().showEvent(ev)

    def _dev(self, mac):
        return self.session["devices"][mac]

    def _save_device(self, mac, **fields):
        if self.session is None:
            return
        self._dev(mac).update(fields)
        self.cfg.save()

    def _run(self, what, macs, fn, on_result, on_done=None):
        """Run fn(mac, progress) for each Shimmer in turn, in the background."""
        if self.busy or not macs:
            return
        self.busy = what

        def work():
            for mac in macs:
                ui_call(self._progress, mac, "waiting…")
            for mac in macs:
                try:
                    res, err = fn(mac, lambda text, m=mac: ui_call(self._progress, m, text)), None
                except Exception as e:                    # noqa: BLE001 - reported per Shimmer
                    res, err = None, e
                ui_call(on_result, mac, res, err)

        def done(_):
            self.busy = ""
            self.progress.clear()
            if on_done:
                on_done()
            self._render()

        def failed(e):
            self.busy = ""
            self.progress.clear()
            log.info("%s stopped: %s", what, e)
            self._render()

        run_bg(work, done, failed, f"sd-{what}")
        self._render()

    def _progress(self, mac, text):
        self.progress[mac] = text
        self._render()

    # ---------------- start / check / stop ----------------
    def _start(self, macs):
        s = self.session
        name, ct = s["name"], s["config_time"]

        def fn(mac, progress):
            return sdlog.start_device(mac, self.cfg, name, ct, progress)

        def result(mac, res, err):
            self.progress.pop(mac, None)
            if isinstance(err, sdlog.StartUnconfirmed):     # may well be recording: never "failed"
                self._save_device(mac, state=sdlog.UNCONFIRMED, error="", start=err.result,
                                  firmware=err.result.get("firmware", ""), note=str(err))
            elif err:
                self._save_device(mac, state=sdlog.FAILED, error=f"{self._dev(mac)['name']} {err}")
            else:
                self._save_device(mac, state=sdlog.RECORDING, error="", start=res, firmware=res["firmware"])
            self._render()
        self._run("start", macs, fn, result)

    def _retry(self):
        self._start([m for m, d in self.session["devices"].items() if d.get("state") == sdlog.FAILED])

    def _check_all(self):
        macs = [m for m, d in self.session["devices"].items()
                if d.get("state") in sdlog.MAY_RECORD + (sdlog.STOPPED,)]

        def result(mac, res, err):
            self.progress.pop(mac, None)
            if err:
                self._save_device(mac, last_check={"ok": False, "error": f"{err}", "when": clocks.unix_ns()})
            else:
                st = res["status"]
                state = self._dev(mac).get("state")
                if st["logging"] and state in (sdlog.UNCONFIRMED, "starting"):
                    self._save_device(mac, state=sdlog.RECORDING, note="")          # confirmed now
                elif not st["logging"] and state == sdlog.UNCONFIRMED:
                    self._save_device(mac, state=sdlog.FAILED, error="was told to start, but is not recording")
                self._save_device(mac, last_check={"ok": True, "logging": st["logging"], "docked": st["docked"],
                                                   "sd_error": st["sd_error"], "offset": res["offset"],
                                                   "when": clocks.unix_ns()})
            self._render()
        self._run("check", macs, lambda mac, progress: sdlog.check_device(mac, self.cfg), result)

    def _stop_all(self):
        macs = [m for m, d in self.session["devices"].items() if d.get("state") in sdlog.MAY_RECORD]

        def fn(mac, progress):
            return sdlog.stop_device(mac, self.cfg, progress)

        def result(mac, res, err):
            self.progress.pop(mac, None)
            if err:
                self._save_device(mac, error=f"could not stop it: {err}")
            else:
                note = "" if res["was_recording"] else ("was not recording any more (docked?)" if res.get("docked")
                                                        else "was not recording any more (stopped with its button?)")
                if res.get("clock_warning"):
                    note = "; ".join(filter(None, (note, res["clock_warning"])))
                self._save_device(mac, state=sdlog.STOPPED, stop=res, error="", note=note)
            self._render()

        def done():
            s = self.session
            if s and not any(d.get("state") in sdlog.MAY_RECORD for d in s["devices"].values()):
                s["stopped"] = True
                s["stopped_unix_ns"] = clocks.unix_ns()
                self.cfg.save()
                self._update_keypoint_buttons()
        self._run("stop", macs, fn, result, done)

    # ---------------- import ----------------
    def _import(self):
        s = self.session
        if not self.base.units:
            QMessageBox.information(self, "Import from base", "Connect the Shimmer base by USB and wait until "
                                    "the Devices page shows it.")
            return
        still = [d["name"] for d in s["devices"].values() if d.get("state") in sdlog.MAY_RECORD]
        if still and QMessageBox.question(
                self, "Import from base", f"{', '.join(still)} did not confirm the stop. If they were docked while "
                "recording, their last file may be empty.\n\nImport anyway?") != QMessageBox.Yes:
            return
        wanted = [m for m, d in s["devices"].items() if d.get("state") in sdlog.MAY_RECORD + (sdlog.STOPPED,)]
        slots = {m: self.base.locate(m) for m in wanted}
        missing = [s["devices"][m]["name"] for m, sl in slots.items() if sl is None]
        docked = [m for m, sl in slots.items() if sl is not None]
        if not docked:
            QMessageBox.information(self, "Import from base", "None of this session's Shimmers is in the base "
                                    "yet (or the base is still reading its slots). Dock them and try again.")
            return
        if missing and QMessageBox.question(
                self, "Import from base", f"Not in the base: {', '.join(missing)}.\n\nImport the others now? "
                "(The missing ones cannot be added to this recording later.)") != QMessageBox.Yes:
            return
        folder = self.cfg.recordings_dir
        session = dict(s)
        loaded, problems = {}, {}

        def fn(mac, progress):
            import tempfile
            from pathlib import Path
            name = s["devices"][mac]["name"]
            progress("opening its SD card through the base…")
            tmp = Path(tempfile.mkdtemp(prefix="shimmer_sd_"))
            where = slots[mac]
            with where.link.lock, sdlog.DockedCard(where.link, where.slot) as card:
                files = sdlog.session_files(card, s["name"], s["config_time"])
                if not files:
                    raise RuntimeError(f"no files of session {s['name']} on its card")
                progress(f"copying {len(files)} file(s)…")
                copied = sdlog.copy_files(files, tmp)
            progress("reading…")
            data = sdlog.read_files(copied)
            return name, data, copied

        def result(mac, res, err):
            self.progress.pop(mac, None)
            if err:
                problems[mac] = err
                self._save_device(mac, error=f"import failed: {err}")
            else:
                loaded[mac] = res
                if res[1].problems:
                    self._save_device(mac, problems=res[1].problems)
            self._render()

        def done():
            if not loaded:
                QMessageBox.warning(self, "Import from base", "Nothing could be imported:\n\n" +
                                    "\n".join(f"{s['devices'][m]['name']}: {e}" for m, e in problems.items()))
                return
            try:
                rec = sdlog.build_recording(self.session or session, loaded, folder, s.get("values", VALUES_RAW))
            except Exception as e:                        # noqa: BLE001
                QMessageBox.warning(self, "Import from base", f"Could not build the recording: {e}")
                return
            for mac in loaded:
                self._save_device(mac, state=sdlog.IMPORTED)
            dlg = SaveRecordingDialog(self, rec)
            dlg.exec()
            if dlg.saved_path is not None or not rec.exists():
                if all(d.get("state") in (sdlog.IMPORTED, sdlog.FAILED) for d in self.session["devices"].values()):
                    sdlog.end_session(self.cfg)
                    self.back.emit()
            self._render()
        self._run("import", docked, fn, result, done)

    def _discard(self):
        if QMessageBox.question(self, "Forget session", "Forget this SD session in the app?\n\nShimmers that "
                                "are still recording keep recording, and nothing is deleted from the SD cards. "
                                "Keypoints marked here are lost.") != QMessageBox.Yes:
            return
        sdlog.end_session(self.cfg)
        self.back.emit()

    # ---------------- keypoints ----------------
    def _build_keypoints(self):
        while self.kp_bar.count():
            it = self.kp_bar.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for sc in self._shortcuts:
            sc.setEnabled(False)
            sc.deleteLater()
        self._shortcuts, self.kp_buttons = [], []
        for i, k in enumerate(kpmod.load(self.cfg)):
            b = set_role(QPushButton(f"{i + 1}   {k.name}" if i < 9 else k.name), "keypoint")
            b.setStyleSheet(f"QPushButton {{ border-left: 10px solid {k.color}; }}")
            b.setToolTip(f"Mark '{k.name}' now" + (f" (key {i + 1})" if i < 9 else "") + " — while the Shimmers record")
            b.clicked.connect(lambda _=False, kp=k: self.mark(kp))
            self.kp_bar.addWidget(b)
            self.kp_buttons.append(b)
            if i < 9:
                sc = QShortcut(QKeySequence(str(i + 1)), self, context=Qt.WidgetWithChildrenShortcut)
                sc.activated.connect(lambda kp=k: self.mark(kp))
                self._shortcuts.append(sc)
        self._update_keypoint_buttons()

    def _can_mark(self):
        s = self.session
        return bool(s) and not s.get("stopped") and any(d.get("state") in sdlog.MAY_RECORD
                                                       for d in s["devices"].values())

    def _update_keypoint_buttons(self):
        ok = self._can_mark()
        for b in getattr(self, "kp_buttons", []):
            b.setEnabled(ok)

    def mark(self, kp, at=None, source=""):
        """Button, key 1-9, or a TCP message (at = the instant it arrived). Returns the event, None if not recording."""
        if not self._can_mark():
            return None
        ev = sdlog.add_keypoint(self.cfg, kp, at, source)
        self._list_keypoint(ev)
        return ev

    def _list_keypoint(self, ev):
        when = dt.datetime.fromtimestamp(ev["unix_ns"] / 1e9).strftime("%H:%M:%S")
        self.kp_list.insertItem(0, QListWidgetItem(f"{when}   {ev['name']}"
                                                   + (f"   (from {ev['source']})" if ev.get("source") else "")))

    # ---------------- rendering ----------------
    def _render(self):
        s = self.session
        if s is None:
            return
        devs = s["devices"]
        n_rec = sum(d.get("state") in sdlog.MAY_RECORD for d in devs.values())
        started = dt.datetime.fromtimestamp(s["start_unix_ns"] / 1e9)
        elapsed = fmt_duration((clocks.unix_ns() - s["start_unix_ns"]) / 1e9)
        if self.busy == "start":
            head = f"<span style='color:{INFO}'>● starting the Shimmers…</span>"
        elif s.get("stopped"):
            stop = dt.datetime.fromtimestamp(s.get("stopped_unix_ns", s["start_unix_ns"]) / 1e9)
            head = f"<span style='color:{GOOD}'>■ stopped</span> — session <b>{s['name']}</b>, " \
                   f"{started:%H:%M:%S} → {stop:%H:%M:%S}"
        elif n_rec:
            head = f"<span style='color:{CRITICAL}'>● recording on {n_rec} SD card(s)</span> — session " \
                   f"<b>{s['name']}</b>, since {started:%H:%M:%S} ({elapsed})"
        else:
            head = f"<span style='color:{WARNING}'>● nothing is recording</span> — session <b>{s['name']}</b>"
        self.headline.setText(head)
        if s.get("stopped"):
            adv = ("Now put the Shimmers in the base and press <b>Import from base</b>. "
                   "The files stay on their SD cards.")
        elif n_rec:
            adv = ("<b>Do not dock a recording Shimmer</b>: stop first, or its file is lost. You can close this app; "
                   "the Shimmers keep recording and the session is here when you reopen it.")
        else:
            adv = "No Shimmer started. Check they are out of the base and switched on, then <b>Retry failed</b>."
        self.advice.setText(adv)

        idle = not self.busy
        failed = any(d.get("state") == sdlog.FAILED for d in devs.values())
        self.btn_retry.setVisible(failed and not s.get("stopped"))
        self.btn_retry.setEnabled(idle)
        self.btn_check.setEnabled(idle and any(d.get("state") in sdlog.MAY_RECORD + (sdlog.STOPPED,)
                                               for d in devs.values()))
        self.btn_stop.setEnabled(idle and n_rec > 0)
        self.btn_import.setEnabled(idle and any(d.get("state") in sdlog.MAY_RECORD + (sdlog.STOPPED,)
                                                for d in devs.values()))
        self.btn_discard.setEnabled(idle)
        self.btn_back.setEnabled(self.busy != "import")
        self._update_keypoint_buttons()

        t = self.table
        t.setRowCount(len(devs))
        for i, (mac, d) in enumerate(devs.items()):
            state, color = self._state_text(mac, d)
            t.setItem(i, 0, QTableWidgetItem(labels.plain(d["name"], d.get("label", ""))))
            st = QTableWidgetItem(state)
            st.setForeground(QColor(color))
            t.setItem(i, 1, st)
            off = (d.get("last_check") or {}).get("offset") or (d.get("stop") or {}).get("stop_offset") \
                or (d.get("start") or {}).get("start_offset")
            t.setItem(i, 2, QTableWidgetItem(f"{off['offset_ms']:+.0f} ms" if off else "—"))
            det = QTableWidgetItem(self._details(mac, d))
            det.setToolTip(det.text())
            t.setItem(i, 3, det)

    def _state_text(self, mac, d):
        if mac in self.progress:
            return self.progress[mac], INFO
        state = d.get("state")
        found = self.base.locate(mac) if self.base else None
        slot = found and self.base.where(found.unit, found.slot)
        if state == sdlog.RECORDING:
            if slot is not None:
                return f"⚠ in {slot}: docked while recording?", CRITICAL
            return "● recording", CRITICAL
        if state == sdlog.STOPPED:
            return (f"■ stopped · in {slot}" if slot else "■ stopped · dock it to import"), GOOD
        if state == sdlog.UNCONFIRMED:
            return "? started, not confirmed: press Check status", WARNING
        if state == sdlog.FAILED:
            return "✖ did not start", CRITICAL
        if state == sdlog.IMPORTED:
            return "✔ imported", GOOD
        return "starting…", INFO

    def _details(self, mac, d):
        if d.get("error"):
            return d["error"]
        parts = []
        if d.get("note"):
            parts.append(d["note"])
        st = d.get("start") or {}
        if st.get("started_unix"):
            parts.append(f"started {dt.datetime.fromtimestamp(st['started_unix']):%H:%M:%S}")
        if st.get("battery_pct") is not None:
            parts.append(f"battery {st['battery_pct']:.0f}% at start")
        chk = d.get("last_check")
        if chk:
            when = dt.datetime.fromtimestamp(chk["when"] / 1e9).strftime("%H:%M:%S")
            if not chk.get("ok"):
                parts.append(f"check at {when}: no answer ({chk.get('error')})")
            else:
                parts.append(f"check at {when}: " + ("recording" if chk.get("logging") else "NOT recording")
                             + (", SD error" if chk.get("sd_error") else ""))
        if d.get("problems"):
            parts.append("; ".join(d["problems"]))
        return " · ".join(parts)

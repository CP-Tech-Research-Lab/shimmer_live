"""Editor for the keypoint list (start page): predefined keypoints plus custom ones."""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QIntValidator
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QSpinBox, QTableWidget, QVBoxLayout, QWidget)

from .. import keypoints as kpmod
from .. import kp_server
from .common import CRITICAL, MUTED, set_role, style_table

TCP_HELP = """<b>Mark keypoints from another program</b> (a stimulus script, a game, Unreal, …).<br>
Open a TCP connection to this computer and send one line (ending with a newline):<br>
&nbsp;• to a keypoint's own port (in the table): anything, even an empty line, marks that keypoint<br>
&nbsp;• to the port for names or numbers (<b>{b}</b>): the keypoint's name or number, e.g. <code>task start</code>
or <code>5</code> &mdash; one connection can then mark any keypoint<br>
The time is taken the instant the line arrives. The app answers <code>OK &lt;name&gt; &lt;unix ns&gt;</code>,
or <code>ERR &lt;reason&gt;</code> if nothing is recording. Works during a live recording and during an
SD recording. One connection can send many lines.<br><br>
<b>Examples</b><br>
Python: <code>socket.create_connection(("127.0.0.1", {b1})).sendall(b"\\n")</code><br>
Linux shell: <code>echo &gt; /dev/tcp/127.0.0.1/{b1}</code><br>
PowerShell: <code>(New-Object Net.Sockets.TcpClient("127.0.0.1",{b1})).GetStream().Write([byte[]](10),0,1)</code>"""


class KeypointsEditor(QWidget):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.items = kpmod.load(cfg)
        if kpmod.assign_ports(self.items, kp_server.settings(cfg)["base_port"]):
            kpmod.save(cfg, self.items)          # every keypoint gets its own fixed port (once)
        lay = QVBoxLayout(self)
        intro = QLabel("Keypoints mark moments during a recording. In live view each one is a button "
                       "(and keys <b>1–9</b> for the first nine); a click draws a line on every plot and is saved "
                       "with the data of every Shimmer. Other programs can mark them too, over TCP (below).")
        intro.setWordWrap(True)
        set_role(intro, "muted")
        lay.addWidget(intro)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Key", "Colour", "Name", "Port", ""])
        self.table.verticalHeader().setVisible(False)
        style_table(self.table, 42)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.new_name = QLineEdit()
        self.new_name.setPlaceholderText("new keypoint name, e.g. stimulus on")
        self.new_name.setMaxLength(40)
        self.new_name.returnPressed.connect(self._add)
        b = QPushButton("Add keypoint")
        b.setToolTip("Add a keypoint with the name typed on the left; it gets its own colour and button in live view")
        b.clicked.connect(self._add)
        row.addWidget(self.new_name, 1)
        row.addWidget(b)
        lay.addLayout(row)
        lay.addSpacing(6)

        # ---- keypoints from other programs over TCP ----
        self.server = None                   # kp_server.KeypointServer, set by the main window
        st = kp_server.settings(cfg)
        tcp = QHBoxLayout()
        self.tcp_on = QCheckBox("Accept keypoints from other programs (TCP)")
        self.tcp_on.setChecked(st["enabled"])
        self.tcp_on.setToolTip("Another program marks a keypoint by sending a line to a TCP port: hover ⓘ how")
        info = set_role(QLabel("ⓘ how"), "muted")
        self.tcp_info = info
        tcp.addWidget(self.tcp_on)
        tcp.addWidget(info)
        tcp.addStretch(1)
        lay.addLayout(tcp)
        names = QHBoxLayout()
        self.tcp_port_label = QLabel("Port for names or numbers:")
        self.tcp_port = QSpinBox()
        self.tcp_port.setRange(1024, 65000)
        self.tcp_port.setValue(st["base_port"])
        tip = ("Send a keypoint's name or number to this port (e.g. 'task start' or '5'): one connection can mark "
               "any keypoint. Each keypoint also has its own port, in the table.")
        self.tcp_port.setToolTip(tip)
        self.tcp_port_label.setToolTip(tip)
        names.addWidget(self.tcp_port_label)
        names.addWidget(self.tcp_port)
        names.addStretch(1)
        lay.addLayout(names)
        self.tcp_net = QCheckBox("Also from other computers on the network")
        self.tcp_net.setChecked(st["network"])
        self.tcp_net.setToolTip("Off: only programs on this computer can mark keypoints. On: any computer that "
                                "can reach this one (the firewall may ask for permission).")
        lay.addWidget(self.tcp_net)
        self.tcp_status = QLabel()
        self.tcp_status.setWordWrap(True)
        self.tcp_status.setTextFormat(Qt.RichText)
        lay.addWidget(self.tcp_status)
        self.tcp_on.toggled.connect(self._tcp_changed)
        self.tcp_net.toggled.connect(self._tcp_changed)
        self.tcp_port.editingFinished.connect(self._tcp_changed)
        self._render()

    # ---------------- TCP ----------------
    def attach_server(self, server):
        self.server = server
        self.apply_tcp()

    def _tcp_changed(self, *_):
        port = self.tcp_port.value()
        taken = next((k.name for k in self.items if k.port == port), None)
        if taken:
            QMessageBox.information(self, "Port for names or numbers",
                                    f"Port {port} is the port of '{taken}'. Choose another one.")
            self.tcp_port.setValue(kp_server.settings(self.cfg)["base_port"])
            return
        kp_server.save_settings(self.cfg, enabled=self.tcp_on.isChecked(), base_port=port,
                                network=self.tcp_net.isChecked())
        self.apply_tcp()

    def apply_tcp(self):
        """(Re)start the server for the current keypoint list and settings, and show what listens where."""
        st = kp_server.settings(self.cfg)
        b = st["base_port"]
        self.tcp_info.setToolTip(TCP_HELP.format(b=b, b1=self.port_of(0) if self.items else b + 1))
        self.tcp_port.setEnabled(st["enabled"])
        self.tcp_port_label.setEnabled(st["enabled"])
        self.tcp_net.setEnabled(st["enabled"])
        if self.server is not None:
            if st["enabled"]:
                self.server.start(b, kpmod.tcp_ports(self.items, b), st["network"])
            else:
                self.server.stop()
        self._render()
        if not st["enabled"]:
            self.tcp_status.setText(f"<span style='color:{MUTED}'>Off.</span>")
            return
        if self.server is None:
            self.tcp_status.setText("")
            return
        text = (f"<span style='color:{MUTED}'>Listening "
                f"({'this computer and the network' if st['network'] else 'this computer only'}) on each "
                f"keypoint's port in the table (click one to change it) and on port {b} for names or "
                f"numbers.</span>")
        if self.server.errors:
            bad = ", ".join(f"{p} ({why})" for p, why in sorted(self.server.errors.items()))
            text += (f"<br><span style='color:{CRITICAL}'>Not available: {bad}. Another program (or a second copy "
                     f"of this app) uses them: choose other ports.</span>")
        self.tcp_status.setText(text)

    def port_of(self, i):
        return kpmod.tcp_ports(self.items, kp_server.settings(self.cfg)["base_port"])[i]

    def _set_port(self, i, text):
        """A port typed in the table (empty or invalid: the old port stays)."""
        base = kp_server.settings(self.cfg)["base_port"]
        text = text.strip()
        port = int(text) if text.isdigit() and 1024 <= int(text) <= 65535 else None
        if port is None or port == self.items[i].port:
            self._render()
            return
        used = {k.port: f"'{k.name}'" for j, k in enumerate(self.items) if j != i and k.port}
        used[base] = "the port for names or numbers"
        if port in used:
            QMessageBox.information(self, "Keypoint port", f"Port {port} is already used by {used[port]}. "
                                                           "Choose another one.")
            self._render()
            return
        self.items[i].port = port
        self._save()
        self._render()

    def _save(self):
        kpmod.save(self.cfg, self.items)
        if self.server is not None and self.server.running:
            want = {kp_server.settings(self.cfg)["base_port"], *kpmod.tcp_ports(self.items, self.server.base_port)}
            if want != set(self.server.targets):
                self.apply_tcp()                 # a keypoint added, removed or moved to another port

    def _render(self):
        t = self.table
        t.setRowCount(len(self.items))
        for r, k in enumerate(self.items):
            key = QLabel(str(r + 1) if r < 9 else "")
            key.setAlignment(Qt.AlignCenter)
            t.setCellWidget(r, 0, key)
            sw = QPushButton()
            sw.setFixedSize(58, 28)
            sw.setStyleSheet(f"background: {k.color}; border: 1px solid #888; border-radius: 5px; padding: 0;"
                             f" min-height: 0;")
            sw.setToolTip("Change colour")
            sw.clicked.connect(lambda _=False, i=r: self._pick_color(i))
            t.setCellWidget(r, 1, sw)
            if k.builtin:
                name = QLabel(f"{k.name}  <span style='color:{MUTED}'>(predefined)</span>")
            else:
                name = QLineEdit(k.name)
                name.setMaxLength(40)
                name.editingFinished.connect(lambda i=r, w=name: self._rename(i, w.text()))
            t.setCellWidget(r, 2, name)
            on = kp_server.settings(self.cfg)["enabled"]
            port = self.port_of(r)
            pl = QLineEdit(str(port))
            pl.setValidator(QIntValidator(1024, 65535, pl))
            pl.setAlignment(Qt.AlignCenter)
            pl.setFixedWidth(76)
            pl.setEnabled(on)
            pl.setToolTip((f"Another program marks '{k.name}' by sending a line to TCP port {port}.\n"
                           f"Type another port to change it (1024–65535).") if on
                          else "Keypoints over TCP are off")
            if on and self.server is not None and port in self.server.errors:
                pl.setStyleSheet(f"color: {CRITICAL};")
                pl.setToolTip(f"Port {port} is not available: {self.server.errors[port]}. Type another one.")
            # handled after the signal returns: re-rendering deletes this very line edit
            pl.editingFinished.connect(lambda i=r, w=pl: QTimer.singleShot(0, lambda t=w.text(): self._set_port(i, t)))
            t.setCellWidget(r, 3, pl)
            if k.builtin:
                t.setCellWidget(r, 4, QLabel(""))
            else:
                rm = set_role(QPushButton("Remove"), "small")
                rm.setToolTip(f"Remove the keypoint '{k.name}' (recordings already made keep it)")
                rm.clicked.connect(lambda _=False, i=r: self._remove(i))
                t.setCellWidget(r, 4, rm)

    def _pick_color(self, i):
        c = QColorDialog.getColor(QColor(self.items[i].color), self, f"Colour for '{self.items[i].name}'")
        if c.isValid():
            self.items[i].color = c.name()
            self._save()
            self._render()

    def _rename(self, i, text):
        text = text.strip()
        if not text or any(k.name == text for j, k in enumerate(self.items) if j != i):
            self._render()
            return
        self.items[i].name = text
        self._save()

    def _remove(self, i):
        del self.items[i]
        self._save()
        self._render()

    def _add(self):
        name = self.new_name.text().strip()
        if not name:
            return
        if any(k.name == name for k in self.items):
            QMessageBox.information(self, "Keypoints", f"'{name}' already exists.")
            return
        self.items.append(kpmod.Keypoint(name, kpmod.next_color(self.items), False,
                                         kpmod.next_free_port(self.items, kp_server.settings(self.cfg)["base_port"])))
        self.new_name.clear()
        self._save()
        self._render()

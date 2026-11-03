"""Plugins page: choose which plugins run in live view; shows user plugin files and load errors."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout,
                               QWidget)

from ..plugins import registry
from ..plugins.api import KIND_NAMES
from .common import CRITICAL, FONT_M, divider, set_role
from .dialogs import open_folder
from .branding import BrandHeader


class PluginsPage(QWidget):
    back = Signal()

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.entries, self.errors = [], []
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 10, 16, 10)

        bar = QHBoxLayout()
        b = QPushButton("◀  Devices")
        b.setToolTip("Back to the device list")
        b.clicked.connect(self.back.emit)
        bar.addWidget(b)
        bar.addSpacing(8)
        bar.addWidget(BrandHeader("Plugins"))
        bar.addStretch(1)
        b = QPushButton("Open plugins folder")
        b.setToolTip("Put your own plugin .py files here (see PLUGINS.md)")
        b.clicked.connect(lambda: open_folder(registry.user_plugins_dir()))
        bar.addWidget(b)
        b = QPushButton("Reload")
        b.setToolTip("Look for new or changed plugin files in the plugins folder")
        b.clicked.connect(self.reload)
        bar.addWidget(b)
        root.addLayout(bar)
        root.addWidget(divider())

        intro = QLabel("Selected plugins run in live view on every device that provides the signals they need. "
                       "Their results are plotted with the device's signals and can be recorded like any other "
                       "signal (● in the Signals panel). To write your own, see <b>PLUGINS.md</b> and put the .py "
                       f"file in <code>{registry.user_plugins_dir()}</code>.")
        intro.setWordWrap(True)
        intro.setTextInteractionFlags(Qt.TextSelectableByMouse)
        set_role(intro, "muted")
        root.addWidget(intro)

        self.list = QVBoxLayout()
        self.list.setSpacing(8)
        holder = QWidget()
        holder.setLayout(self.list)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(holder)
        root.addWidget(scroll, 1)
        self.reload()

    @property
    def enabled_ids(self) -> set:
        return set(self.cfg.data.get("plugins_enabled", []))

    def enabled_entries(self):
        return [e for e in self.entries if e.cls.id in self.enabled_ids]

    def _set_enabled(self, pid, on):
        ids = self.enabled_ids
        ids.add(pid) if on else ids.discard(pid)
        self.cfg.data["plugins_enabled"] = sorted(ids)
        self.cfg.save()

    def reload(self):
        self.entries, self.errors = registry.discover()
        while self.list.count():
            it = self.list.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for e in self.entries:
            self.list.addWidget(self._card(e))
        for src, msg in self.errors:
            lab = QLabel(f"✖ <b>{src}</b>: {msg}")
            lab.setWordWrap(True)
            lab.setStyleSheet(f"color: {CRITICAL}; font-weight: 600;")
            self.list.addWidget(lab)
        self.list.addStretch(1)

    def _card(self, e):
        c = e.cls
        card = QFrame()
        card.setStyleSheet("QFrame#card { background: white; border: 1px solid #dcdcd8; border-radius: 8px; }")
        card.setObjectName("card")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(6)
        head = QHBoxLayout()
        cb = QCheckBox(f"{c.name}")
        cb.setStyleSheet(f"font-weight: 700; font-size: {FONT_M}pt;")
        cb.setChecked(c.id in self.enabled_ids)
        cb.toggled.connect(lambda on, pid=c.id: self._set_enabled(pid, on))
        head.addWidget(cb)
        meta = QLabel(f"v{c.version} · {e.source}")
        set_role(meta, "muted")
        head.addStretch(1)
        head.addWidget(meta)
        v.addLayout(head)
        d = QLabel(c.description)
        d.setWordWrap(True)
        v.addWidget(d)
        ins = ", ".join(KIND_NAMES.get(i.kind, i.kind) + (f" ≥ {i.min_rate:g} Hz" if i.min_rate else "") +
                        (f" ({i.description})" if i.description else "") for i in c.inputs)
        outs = ", ".join(f"{o.label} [{o.unit}]" if o.unit else o.label for o in c.outputs)
        io = QLabel(f"<b>Needs:</b> {ins or '—'}<br><b>Produces:</b> {outs or '—'}")
        set_role(io, "muted")
        v.addWidget(io)
        return card

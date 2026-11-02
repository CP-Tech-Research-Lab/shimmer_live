"""Editor for the keypoint list (start page): predefined keypoints plus custom ones."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QColorDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
                               QTableWidget, QVBoxLayout, QWidget)

from .. import keypoints as kpmod
from .common import MUTED


class KeypointsEditor(QWidget):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.items = kpmod.load(cfg)
        lay = QVBoxLayout(self)
        intro = QLabel("Keypoints mark moments during a recording. In live view each one is a button "
                       "(and keys <b>1–9</b> for the first nine); a click draws a line on every plot and is saved "
                       "with the data of every Shimmer.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {MUTED};")
        lay.addWidget(intro)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Key", "Colour", "Name", ""])
        self.table.verticalHeader().setVisible(False)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.new_name = QLineEdit()
        self.new_name.setPlaceholderText("new keypoint name, e.g. stimulus on")
        self.new_name.setMaxLength(40)
        self.new_name.returnPressed.connect(self._add)
        b = QPushButton("Add keypoint")
        b.clicked.connect(self._add)
        row.addWidget(self.new_name, 1)
        row.addWidget(b)
        lay.addLayout(row)
        self._render()

    def _save(self):
        kpmod.save(self.cfg, self.items)

    def _render(self):
        t = self.table
        t.setRowCount(len(self.items))
        for r, k in enumerate(self.items):
            key = QLabel(str(r + 1) if r < 9 else "")
            key.setAlignment(Qt.AlignCenter)
            t.setCellWidget(r, 0, key)
            sw = QPushButton()
            sw.setFixedSize(46, 22)
            sw.setStyleSheet(f"background: {k.color}; border: 1px solid #888; border-radius: 3px;")
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
            if k.builtin:
                t.setCellWidget(r, 3, QLabel(""))
            else:
                rm = QPushButton("Remove")
                rm.clicked.connect(lambda _=False, i=r: self._remove(i))
                t.setCellWidget(r, 3, rm)

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
        self.items.append(kpmod.Keypoint(name, kpmod.next_color(self.items), False))
        self.new_name.clear()
        self._save()
        self._render()

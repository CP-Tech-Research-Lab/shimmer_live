"""A wheel / two-finger scroll over the live plots scrolls the plot list; it never zooms a y axis."""
import os

import pytest


def test_scroll_over_plots_scrolls_the_list_and_does_not_zoom():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    app = widgets.QApplication.instance() or widgets.QApplication([])
    from shimmer_live.ui.live_page import PlotsView
    view = PlotsView()
    plots = [view.addPlot(row=i, col=0) for i in range(6)]
    for p in plots:
        p.setMouseEnabled(x=False, y=False)
        p.plot([0, 1, 2], [0, 1, 0])
        p.disableAutoRange()
        p.setYRange(0, 1, padding=0)
    view.setMinimumHeight(1800)
    area = widgets.QScrollArea()
    area.setWidgetResizable(True)
    area.setWidget(view)
    area.resize(600, 400)
    area.show()
    app.processEvents()
    before = plots[0].vb.viewRange()[1]
    pos = QPointF(300, 150)
    ev = QWheelEvent(pos, QPointF(view.viewport().mapToGlobal(QPoint(300, 150))), QPoint(0, -120), QPoint(0, -120),
                     Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    app.sendEvent(view.viewport(), ev)
    app.processEvents()
    assert area.verticalScrollBar().value() > 0                 # the list scrolled down
    assert plots[0].vb.viewRange()[1] == before                  # no zoom
    area.close()

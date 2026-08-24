"""Qt binding compatibility shim.

Imports PyQt6 if available, otherwise PyQt5, and exposes a single normalized set
of enum constants so the rest of the GUI never has to branch on the binding.

Usage:  from .qt import *
"""
from __future__ import annotations

try:
    from PyQt6.QtCore import *          # noqa: F401,F403
    from PyQt6.QtGui import *           # noqa: F401,F403
    from PyQt6.QtWidgets import *       # noqa: F401,F403
    from PyQt6.QtCore import Qt, QThread, QObject, QTimer, pyqtSignal
    BINDING = "PyQt6"
except ImportError:  # pragma: no cover - fallback path
    from PyQt5.QtCore import *          # noqa: F401,F403
    from PyQt5.QtGui import *           # noqa: F401,F403
    from PyQt5.QtWidgets import *       # noqa: F401,F403
    from PyQt5.QtCore import Qt, QThread, QObject, QTimer, pyqtSignal
    BINDING = "PyQt5"


# --------------------------------------------------------- normalized enums
if BINDING == "PyQt6":
    ALIGN_CENTER = Qt.AlignmentFlag.AlignCenter
    ALIGN_LEFT = Qt.AlignmentFlag.AlignLeft
    ALIGN_RIGHT = Qt.AlignmentFlag.AlignRight
    ALIGN_VCENTER = Qt.AlignmentFlag.AlignVCenter
    ORIENT_H = Qt.Orientation.Horizontal
    ORIENT_V = Qt.Orientation.Vertical
    RESIZE_STRETCH = QHeaderView.ResizeMode.Stretch            # noqa: F405
    RESIZE_CONTENTS = QHeaderView.ResizeMode.ResizeToContents  # noqa: F405
    RESIZE_INTERACTIVE = QHeaderView.ResizeMode.Interactive    # noqa: F405
    SELECT_ROWS = QAbstractItemView.SelectionBehavior.SelectRows      # noqa: F405
    SELECT_SINGLE = QAbstractItemView.SelectionMode.SingleSelection   # noqa: F405
    FRAME_HLINE = QFrame.Shape.HLine                          # noqa: F405
    FRAME_SUNKEN = QFrame.Shadow.Sunken                       # noqa: F405
    SP_EXPANDING = QSizePolicy.Policy.Expanding               # noqa: F405
    SP_PREFERRED = QSizePolicy.Policy.Preferred               # noqa: F405
    TAB_NORTH = QTabWidget.TabPosition.North                  # noqa: F405
    SCROLLBAR_ASNEEDED = Qt.ScrollBarPolicy.ScrollBarAsNeeded
    EDIT_NONE = QAbstractItemView.EditTrigger.NoEditTriggers  # noqa: F405
else:  # PyQt5
    ALIGN_CENTER = Qt.AlignCenter
    ALIGN_LEFT = Qt.AlignLeft
    ALIGN_RIGHT = Qt.AlignRight
    ALIGN_VCENTER = Qt.AlignVCenter
    ORIENT_H = Qt.Horizontal
    ORIENT_V = Qt.Vertical
    RESIZE_STRETCH = QHeaderView.Stretch                      # noqa: F405
    RESIZE_CONTENTS = QHeaderView.ResizeToContents            # noqa: F405
    RESIZE_INTERACTIVE = QHeaderView.Interactive              # noqa: F405
    SELECT_ROWS = QAbstractItemView.SelectRows               # noqa: F405
    SELECT_SINGLE = QAbstractItemView.SingleSelection        # noqa: F405
    FRAME_HLINE = QFrame.HLine                               # noqa: F405
    FRAME_SUNKEN = QFrame.Sunken                             # noqa: F405
    SP_EXPANDING = QSizePolicy.Expanding                     # noqa: F405
    SP_PREFERRED = QSizePolicy.Preferred                     # noqa: F405
    TAB_NORTH = QTabWidget.North                             # noqa: F405
    SCROLLBAR_ASNEEDED = Qt.ScrollBarAsNeeded
    EDIT_NONE = QAbstractItemView.NoEditTriggers             # noqa: F405


def run_app_exec(app):
    """exec() is the PyQt6 name; PyQt5 also supports it in current versions."""
    if hasattr(app, "exec"):
        return app.exec()
    return app.exec_()  # very old PyQt5

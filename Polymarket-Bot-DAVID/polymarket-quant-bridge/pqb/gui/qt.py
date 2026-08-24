"""
Qt compatibility shim — PyQt6 preferred, PyQt5 accepted.

Same approach as the Quant Bridge's own desktop app, for the same reason: the
two bindings differ mainly in where enums live, and a client PC may already
have one or the other. Failing to start because it found the "wrong" Qt would
be a poor reason to lose an evening.

Importing this module is also the single place that knows the dashboard needs
Qt at all — the trading loop, the CLI and the server deployment never touch it,
which is why PyQt6 stays out of the main requirements file.
"""

from __future__ import annotations

QT_BINDING = ""

try:  # PyQt6
    from PyQt6.QtCore import QTimer, Qt                     # noqa: F401
    from PyQt6.QtGui import QAction, QColor, QFont          # noqa: F401
    from PyQt6.QtWidgets import (                           # noqa: F401
        QApplication, QCheckBox, QGridLayout, QGroupBox, QHBoxLayout,
        QHeaderView, QDoubleSpinBox, QLabel, QMainWindow, QMessageBox,
        QPlainTextEdit,
        QPushButton, QScrollArea, QSplitter, QTableWidget, QTableWidgetItem,
        QTabWidget, QVBoxLayout,
        QWidget,
    )
    QT_BINDING = "PyQt6"

    # Named constants rather than the enum spelling at the call site: PyQt5
    # keeps these flat on Qt and PyQt6 nests them, and Qt itself refuses new
    # attributes, so the two spellings cannot be reconciled by patching Qt.
    VERTICAL = Qt.Orientation.Vertical
    ALIGN_TOP = Qt.AlignmentFlag.AlignTop
except ImportError:  # pragma: no cover - depends on what is installed
    try:
        from PyQt5.QtCore import QTimer, Qt                 # noqa: F401
        from PyQt5.QtGui import QColor, QFont               # noqa: F401
        from PyQt5.QtWidgets import (                       # noqa: F401
            QAction, QApplication, QCheckBox, QDoubleSpinBox, QGridLayout,
            QGroupBox,
            QHBoxLayout, QHeaderView, QLabel, QMainWindow, QMessageBox,
            QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QTableWidget,
            QTableWidgetItem,
            QTabWidget, QVBoxLayout, QWidget,
        )
        QT_BINDING = "PyQt5"

        VERTICAL = Qt.Vertical
        ALIGN_TOP = Qt.AlignTop

        # PyQt5 keeps these enums on the classes rather than nested; the
        # dashboard is written against the PyQt6 spelling, so map them here
        # instead of branching at every call site.
        class _Compat:
            pass

        QTableWidget.EditTrigger = _Compat()
        QTableWidget.EditTrigger.NoEditTriggers = QTableWidget.NoEditTriggers
        QTableWidget.SelectionBehavior = _Compat()
        QTableWidget.SelectionBehavior.SelectRows = QTableWidget.SelectRows
        QHeaderView.ResizeMode = _Compat()
        QHeaderView.ResizeMode.Stretch = QHeaderView.Stretch
        QMessageBox.ButtonRole = _Compat()
        QMessageBox.ButtonRole.AcceptRole = QMessageBox.AcceptRole
        QMessageBox.ButtonRole.RejectRole = QMessageBox.RejectRole
        QMessageBox.ButtonRole.DestructiveRole = QMessageBox.DestructiveRole
    except ImportError as exc:
        raise ImportError(
            "The dashboard needs PyQt6 (or PyQt5).\n\n"
            "Install it with:\n"
            "    .venv\\Scripts\\python -m pip install PyQt6\n\n"
            "The bot itself does not need it — only this window does."
        ) from exc


__all__ = [
    "QAction", "QApplication", "QCheckBox", "QColor", "QDoubleSpinBox", "QFont", "QGridLayout", "QGroupBox",
    "QHBoxLayout", "QHeaderView", "QLabel", "QMainWindow", "QMessageBox",
    "QPlainTextEdit", "QPushButton", "QScrollArea", "QSplitter",
    "QTableWidget", "QTableWidgetItem",
    "QTabWidget", "QTimer", "QVBoxLayout", "QWidget", "Qt", "QT_BINDING",
    "VERTICAL", "ALIGN_TOP",
]

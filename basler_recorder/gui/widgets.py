"""Small shared GUI widgets."""
from __future__ import annotations

from PySide6.QtWidgets import QComboBox


class NoWheelComboBox(QComboBox):
    """QComboBox that does not change selection on accidental wheel events."""

    def wheelEvent(self, event):  # noqa: N802 - Qt override name
        if self.view().isVisible():
            super().wheelEvent(event)
        else:
            event.ignore()

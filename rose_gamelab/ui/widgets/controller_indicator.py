"""A small readout of the pads that are connected, and their charge.

Used in two places with the same behaviour and two sizes: the desktop status
bar, where it sits quietly at the end of the row, and Big Picture, where it has
to be legible from a sofa.

It says nothing at all when no pad is connected. A launcher that permanently
displays "No controller" is nagging someone who is playing with a keyboard on
purpose, and the information is only worth screen space once it is relevant.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QWidget

from rose_gamelab.core.controller_status import ControllerStatus
from rose_gamelab.ui.theme import Theme

# Charge thresholds for the little glyph. Coarse on purpose: the exact
# percentage is in the text beside it, and a five-state icon reads instantly
# from across a room in a way a number does not.
_LEVELS = ((80, "🔋"), (50, "🔋"), (20, "🪫"), (0, "🪫"))


def battery_glyph(percent: Optional[int], charging: Optional[bool]) -> str:
    if charging:
        return "⚡"
    if percent is None:
        return ""
    for threshold, glyph in _LEVELS:
        if percent >= threshold:
            return glyph
    return "🪫"


def describe(statuses: list[ControllerStatus]) -> str:
    """One line for however many pads are connected.

    Two pads with batteries would overflow a status bar, so past one pad the
    count leads and only the lowest battery is shown — which is the one that is
    going to interrupt play.
    """
    if not statuses:
        return ""

    if len(statuses) == 1:
        status = statuses[0]
        battery = status.battery
        if battery and battery.percent is not None:
            glyph = battery_glyph(battery.percent, battery.charging)
            return f"🎮 {status.name}  {glyph} {battery.percent}%"
        return f"🎮 {status.name}"

    charged = [
        s.battery.percent for s in statuses
        if s.battery and s.battery.percent is not None
    ]
    if charged:
        lowest = min(charged)
        return f"🎮 {len(statuses)} controllers  {battery_glyph(lowest, False)} {lowest}%"
    return f"🎮 {len(statuses)} controllers"


class ControllerIndicator(QLabel):
    """Shows the connected pads. Hidden entirely when there are none."""

    def __init__(
        self,
        theme: Theme,
        parent: Optional[QWidget] = None,
        *,
        font_size: int = 12,
    ) -> None:
        super().__init__(parent)

        self.theme = theme
        self._font_size = font_size
        self._statuses: list[ControllerStatus] = []

        self.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
        self._restyle()
        self.hide()

    def apply_theme(self, theme: Theme) -> None:
        self.theme = theme
        self._restyle()

    def _restyle(self) -> None:
        colour = self.theme.text_dim
        if self._low():
            # The one case worth pulling the eye: a pad about to die.
            colour = self.theme.warning
        self.setStyleSheet(f"color: {colour}; font-size: {self._font_size}px;")

    def _low(self) -> bool:
        return any(s.battery and s.battery.low and not s.battery.charging
                   for s in self._statuses)

    def set_statuses(self, statuses: list[ControllerStatus]) -> None:
        self._statuses = list(statuses)

        text = describe(self._statuses)
        self.setText(text)
        self.setToolTip("\n".join(s.label for s in self._statuses))
        self._restyle()
        self.setVisible(bool(text))

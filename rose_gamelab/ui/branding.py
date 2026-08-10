"""Rose GameLab branding.

The rose ASCII art is "rose (3/99)" by Joan G. Stark ("jgs"), an American ASCII
artist active 1996-2003 (https://en.wikipedia.org/wiki/Joan_Stark), from her
archived gallery (github.com/oldcompcz/jgs). Her signature line has been
removed from the displayed art; credit is preserved here and in the README.

The same rose appears in every Rose Open Source Endeavours project. It is
always drawn white for the bloom and green for the stem, independent of the
active theme, so the mark stays recognisable across projects.
"""

from __future__ import annotations

# "rose (3/99)" by Joan G. Stark (jgs) — signature line blanked out.
ROSE_ART = r"""
         _
      _.;_'-._
     {`--.-'_,}
    {; \,__.-'/}
    {.'-`._;-';
     `'--._.-'
        .-\\,-"-.
        `- \( '-. \
            \;---,/
        .-""-;\
       /  .-' )\
       \,---'` \\
                \|"""

# Lines 0-5 are the bloom; 6 onwards are the stem and leaves.
ROSE_STEM_START_LINE = 6
ROSE_GREEN = "#3FA860"
ROSE_WHITE = "#FFFFFF"

APP_NAME = "Rose GameLab"
APP_TAGLINE = "Every game you own, in one place."
ORGANISATION = "Rose Open Source Endeavours"


def rose_lines() -> list[str]:
    """The art as a list of lines, with the leading blank line removed."""
    return ROSE_ART.strip("\n").split("\n")


def rose_widget(petal: str = ROSE_WHITE, stem: str = ROSE_GREEN):
    """The rose as a widget, drawn in plain monospace text.

    Deliberately NOT rich text with a centred alignment. ASCII art depends on
    every line keeping its exact leading whitespace, and a centred rich-text
    label centres each LINE independently — which shifts every row by a
    different amount and tears the drawing apart. HTML line-height is also
    ignored by Qt's rich text engine, which stretched the art vertically.

    So: two plain-text labels (bloom, then stem), left-aligned internally so
    the spacing is untouched, and centred as a BLOCK by the layout instead.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

    lines = rose_lines()
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setPointSizeF(9.5)
    font.setStyleHint(QFont.StyleHint.Monospace)

    block = QWidget()
    column = QVBoxLayout(block)
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(0)

    for start, end, colour in (
        (0, ROSE_STEM_START_LINE, petal),
        (ROSE_STEM_START_LINE, len(lines), stem),
    ):
        label = QLabel("\n".join(lines[start:end]))
        label.setFont(font)
        label.setTextFormat(Qt.TextFormat.PlainText)
        # Left-aligned: the art's own leading spaces do the positioning.
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        label.setStyleSheet(f"color: {colour}; background: transparent;")
        column.addWidget(label)

    # Centre the whole drawing without touching the text inside it.
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.addStretch(1)
    row.addWidget(block)
    row.addStretch(1)

    return holder

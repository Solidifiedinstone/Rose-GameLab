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


def rose_html(petal: str = ROSE_WHITE, stem: str = ROSE_GREEN) -> str:
    """The rose as colourised HTML, for Qt rich-text labels.

    Rendered as HTML rather than plain text because the art must keep its
    exact spacing while carrying two colours, and Qt labels do not support
    per-line colour any other way.
    """
    from html import escape

    rows = []
    for index, line in enumerate(rose_lines()):
        colour = stem if index >= ROSE_STEM_START_LINE else petal
        rows.append(f'<span style="color:{colour}">{escape(line)}</span>')

    return (
        '<div style="font-family:monospace;white-space:pre;line-height:1.0">'
        + "<br>".join(rows)
        + "</div>"
    )

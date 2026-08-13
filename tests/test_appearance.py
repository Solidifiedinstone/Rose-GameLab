"""Themes, styles, and the ability to combine them freely.

Colour and shape are deliberately separate. The tests that matter most here are
the ones proving they stay separate: any theme with any style, and any single
aspect adjustable without disturbing the rest.

Nothing here needs a running Qt application — a stylesheet is a string.
"""

from __future__ import annotations

import json

import pytest

from rose_gamelab.ui.preferences import STYLE_AXES, STYLE_RANGES, Preferences
from rose_gamelab.ui.theme import (
    COVER_WIDTHS,
    DEFAULT_STYLE,
    DEFAULT_THEME,
    STYLES,
    THEMES,
    Appearance,
    Style,
    Theme,
    get_style,
    get_theme,
    list_style_names,
    list_theme_names,
    stylesheet,
)

# ── The registries ────────────────────────────────────────────────

def test_there_are_plenty_of_themes():
    assert len(THEMES) >= 20


def test_there_are_plenty_of_styles():
    assert len(STYLES) >= 8


def test_every_theme_is_complete():
    """A missing colour renders as an empty string and breaks the stylesheet."""
    for key, theme in THEMES.items():
        for name, value in theme.to_dict().items():
            assert value, f"{key} has no {name}"
            if name != "name":
                assert value.startswith("#"), f"{key}.{name} is not a colour"


def test_every_theme_has_a_distinct_name():
    names = [theme.name for theme in THEMES.values()]
    assert len(names) == len(set(names))


def test_light_and_dark_are_both_offered():
    """Light themes must actually be light, not mislabelled."""
    def brightness(colour: str) -> int:
        return sum(int(colour[i:i + 2], 16) for i in (1, 3, 5))

    light = [k for k, t in THEMES.items() if brightness(t.background) > 500]
    dark = [k for k, t in THEMES.items() if brightness(t.background) < 200]
    assert len(light) >= 4 and len(dark) >= 10


def test_high_contrast_themes_are_actually_high_contrast():
    """Not a stylistic choice — these exist for people the others fail."""
    for key in ("high-contrast", "high-contrast-light"):
        theme = THEMES[key]
        assert {theme.text, theme.background} in (
            {"#ffffff", "#000000"},
        ), f"{key} does not use maximum contrast for body text"


@pytest.mark.parametrize("key", sorted(THEMES))
def test_every_theme_renders(key):
    assert "border-radius" in stylesheet(THEMES[key])


@pytest.mark.parametrize("key", sorted(STYLES))
def test_every_style_renders(key):
    assert "border-radius" in stylesheet(THEMES[DEFAULT_THEME], STYLES[key])


def test_unknown_names_fall_back_rather_than_raising():
    assert get_theme("does-not-exist") is THEMES[DEFAULT_THEME]
    assert get_style("does-not-exist") is STYLES[DEFAULT_STYLE]


def test_names_are_listed_for_the_picker():
    assert ("rose-dark", "Rose Dark") in list_theme_names()
    assert ("sharp", "Sharp") in list_style_names()


# ── Mixing and matching ───────────────────────────────────────────

def test_any_theme_composes_with_any_style():
    """The whole point: no combination is special-cased or forbidden."""
    for theme_key in THEMES:
        for style_key in STYLES:
            css = stylesheet(THEMES[theme_key], STYLES[style_key])
            assert THEMES[theme_key].background in css
            assert f"border-radius: {STYLES[style_key].radius}px" in css


def test_style_changes_shape_without_touching_colour():
    theme = THEMES["gruvbox"]
    sharp = stylesheet(theme, STYLES["sharp"])
    soft = stylesheet(theme, STYLES["soft"])

    assert sharp != soft
    assert theme.accent in sharp and theme.accent in soft


def test_theme_changes_colour_without_touching_shape():
    style = STYLES["compact"]
    nord = stylesheet(THEMES["nord"], style)
    mocha = stylesheet(THEMES["catppuccin-mocha"], style)

    assert f"border-radius: {style.radius}px" in nord
    assert f"border-radius: {style.radius}px" in mocha
    assert THEMES["nord"].accent not in mocha


def test_one_aspect_can_be_overridden_on_its_own():
    style = get_style("soft").with_overrides(radius=0)

    assert style.radius == 0
    # Everything else still comes from Soft.
    assert style.spacing == STYLES["soft"].spacing
    assert style.font_size == STYLES["soft"].font_size


def test_overriding_with_none_leaves_the_value_alone():
    """Callers pass sparse preference dicts straight through."""
    style = get_style("soft").with_overrides(radius=None, font_size=20)

    assert style.radius == STYLES["soft"].radius
    assert style.font_size == 20


def test_unknown_axes_are_ignored_not_fatal():
    assert get_style("soft").with_overrides(nonsense=1) == STYLES["soft"]


def test_compose_builds_a_full_appearance():
    appearance = Appearance.compose("nord", "sharp", font_size=20)

    assert appearance.theme.name == "Nord"
    assert appearance.style.name == "Sharp"
    assert appearance.style.radius == 0
    assert "font-size: 20px" in appearance.stylesheet()


def test_borderless_styles_really_have_no_border():
    css = stylesheet(THEMES[DEFAULT_THEME], STYLES["flat"])
    assert "border: 0px solid" in css


def test_flat_styles_do_not_raise_panels():
    """Panels share the window's background, so only spacing separates them."""
    theme = THEMES["tokyo-night"]
    css = stylesheet(theme, STYLES["flat"])

    assert f"#DetailPanel {{\n        background-color: {theme.background}" in css


# ── Round-tripping ────────────────────────────────────────────────

def test_a_style_survives_json():
    style = STYLES["console"]
    assert Style.from_dict(json.loads(json.dumps(style.to_dict()))) == style


def test_a_theme_survives_a_file(tmp_path):
    path = tmp_path / "mine.json"
    THEMES["dracula"].save(path)
    assert Theme.load(path) == THEMES["dracula"]


def test_unknown_keys_from_a_newer_version_are_dropped():
    assert Style.from_dict({"radius": 4, "from_the_future": True}).radius == 4
    assert Theme.from_dict({"accent": "#fff", "glow": "yes"}).accent == "#fff"


def test_an_appearance_survives_json():
    appearance = Appearance.compose("kanagawa", "outlined", spacing=30)
    restored = Appearance.from_dict(json.loads(json.dumps(appearance.to_dict())))

    assert restored.theme == appearance.theme
    assert restored.style == appearance.style


# ── Preferences ───────────────────────────────────────────────────

def test_preferences_default_to_the_defaults():
    prefs = Preferences()
    assert prefs.appearance().theme is THEMES[DEFAULT_THEME]
    assert prefs.appearance().style is STYLES[DEFAULT_STYLE]


def test_preferences_survive_a_restart(tmp_path):
    """The bug this exists for: the theme picker used to be forgotten on exit."""
    path = tmp_path / "preferences.json"

    prefs = Preferences(theme="nord", style="sharp")
    prefs.override("font_size", 20)
    prefs.save(path)

    restored = Preferences.load(path)

    assert restored.theme == "nord"
    assert restored.style == "sharp"
    assert restored.appearance().style.font_size == 20
    assert restored.appearance().style.radius == 0


def test_missing_preferences_are_not_an_error(tmp_path):
    assert Preferences.load(tmp_path / "nothing.json").theme == DEFAULT_THEME


def test_corrupt_preferences_fall_back_to_defaults(tmp_path):
    """A truncated file must not stop the launcher from starting."""
    path = tmp_path / "preferences.json"
    path.write_text("{not json at all")

    assert Preferences.load(path).theme == DEFAULT_THEME


def test_a_theme_that_no_longer_exists_falls_back(tmp_path):
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"theme": "retired-theme", "style": "gone"}))

    prefs = Preferences.load(path)
    assert prefs.theme == DEFAULT_THEME
    assert prefs.style == DEFAULT_STYLE


def test_out_of_range_overrides_are_clamped():
    """A slider must not be able to produce an unusable, unfixable interface."""
    prefs = Preferences()
    prefs.override("font_size", 900)

    _low, high = STYLE_RANGES["font_size"]
    assert prefs.appearance().style.font_size == high


def test_an_unknown_axis_is_refused():
    with pytest.raises(KeyError):
        Preferences().override("chrome_finish", "matte")


def test_an_unknown_cover_size_is_refused():
    with pytest.raises(ValueError):
        Preferences().override("cover_size", "enormous")


def test_clearing_an_override_returns_to_the_style():
    prefs = Preferences(style="soft")
    prefs.override("radius", 0)
    assert prefs.appearance().style.radius == 0

    prefs.override("radius", None)
    assert prefs.appearance().style.radius == STYLES["soft"].radius


def test_overrides_only_store_what_changed():
    """So a style that improves in a later release improves for the user too."""
    prefs = Preferences(style="soft")
    prefs.override("radius", 3)

    assert prefs.to_dict()["style_overrides"] == {"radius": 3}


def test_every_adjustable_axis_is_a_real_style_field():
    assert set(STYLE_AXES) <= set(Style.__dataclass_fields__)


def test_every_numeric_axis_has_a_range():
    numeric = {a for a, (_label, kind) in STYLE_AXES.items() if kind == "int"}
    assert numeric == set(STYLE_RANGES)


def test_cover_sizes_offered_are_real():
    prefs = Preferences()
    for name in COVER_WIDTHS:
        prefs.override("cover_size", name)
        assert prefs.appearance().style.cover_size == name


# ── Startup behaviour ─────────────────────────────────────────────

def test_startup_scanning_can_be_turned_off(tmp_path):
    """A settled library does not need checking every single launch."""
    from rose_gamelab.ui.preferences import Preferences

    prefs = Preferences()
    assert prefs.scan_on_start          # on by default: a launcher should find new games
    assert prefs.art_on_start

    prefs.scan_on_start = False
    prefs.save(tmp_path / "prefs.json")

    restored = Preferences.load(tmp_path / "prefs.json")
    assert restored.scan_on_start is False
    assert restored.art_on_start is True     # the two are independent


def test_art_fetching_is_a_separate_choice(tmp_path):
    from rose_gamelab.ui.preferences import Preferences

    prefs = Preferences(scan_on_start=True, art_on_start=False)
    prefs.save(tmp_path / "prefs.json")

    restored = Preferences.load(tmp_path / "prefs.json")
    assert restored.scan_on_start is True
    assert restored.art_on_start is False


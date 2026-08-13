"""The version GameLab reports.

Caught during the 0.2.0 release: the package was built as 0.2.0, installed as
0.2.0, and `rose-gamelab --version` said 0.1.0 — because the number lived in
two places, `pyproject.toml` and `__init__.py`, and only one had been changed.
A release that misreports itself makes every bug report ambiguous, so the two
cannot be allowed to disagree again.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import rose_gamelab

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def declared_version() -> str:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def test_the_reported_version_matches_the_packaged_one():
    assert rose_gamelab.__version__ == declared_version()


def test_the_version_looks_like_a_version():
    assert re.fullmatch(r"\d+\.\d+\.\d+.*", rose_gamelab.__version__)


def test_the_cli_reports_the_same_version():
    """`--version` is what a bug report quotes, so run it rather than inspect it."""
    from click.testing import CliRunner

    from rose_gamelab.main import main

    result = CliRunner().invoke(main, ["--version"])

    assert result.exit_code == 0
    assert declared_version() in result.output


# ── What GameLab tells servers it is ──────────────────────────────

def test_the_user_agent_carries_the_real_version():
    """It said 0.1 through the whole of 0.1 and 0.2 — a hardcoded literal that
    nothing updated and nothing checked."""
    from rose_gamelab.metadata.base import USER_AGENT

    assert declared_version() in USER_AGENT
    assert "Rose-GameLab/" in USER_AGENT


def test_the_user_agent_actually_reaches_the_session():
    """Regression: this was applied with `setdefault`, and requests populates
    its own User-Agent when a Session is constructed — so setdefault always
    lost and the descriptive name never left the machine."""
    from rose_gamelab.metadata.base import USER_AGENT
    from rose_gamelab.metadata.retroachievements import RetroAchievementsProvider

    provider = RetroAchievementsProvider(username="x", api_key="y")

    assert provider.session.headers["User-Agent"] == USER_AGENT


def test_no_provider_hardcodes_a_version():
    """One copy of the string, derived from one source."""
    import re
    from pathlib import Path

    package = Path(__file__).resolve().parent.parent / "rose_gamelab"
    offenders = [
        path.name for path in package.rglob("*.py")
        if re.search(r"Rose-GameLab/\d", path.read_text(encoding="utf-8"))
        and path.name != "base.py"
    ]
    assert offenders == []

"""Rose GameLab — Your games, one launcher."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version

# Read from the installed package rather than written here. These were two
# separate literals — this one and the version in pyproject.toml — and they
# drifted: 0.2.0 was built, installed, and reported itself as 0.1.0, because
# nothing made the two agree and nothing checked.
#
# pyproject.toml is the single source now. An editable install carries metadata
# too, so this is correct for development as well.
try:
    __version__ = _installed_version("rose-gamelab")
except PackageNotFoundError:  # running from a source tree, never installed
    __version__ = "0.0.0+source"

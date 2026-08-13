"""Installing RetroArch and fetching libretro cores.

Nothing here touches the network: downloads go through a fake session. What is
tested is the part that goes wrong quietly — writing a core somewhere RetroArch
will never look, or leaving a half-written file that RetroArch tries to load.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from rose_gamelab.core import retroarch
from rose_gamelab.core.library import Library
from rose_gamelab.db.database import Database


@pytest.fixture
def library(tmp_path):
    database = Database(tmp_path / "library.db")
    yield Library(database)
    database.close()


def make_zip(name="snes9x_libretro.so", size=None):
    payload = b"\x7fELF" + b"\x00" * (size or retroarch.MIN_CORE_BYTES)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(name, payload)
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, content=b"", status=200):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"status {self.status_code}")


class FakeSession:
    """Serves archives by core name, and 404s for anything else."""

    def __init__(self, archives=None):
        self.archives = archives or {}
        self.headers: dict[str, str] = {}
        self.requested: list[str] = []

    def get(self, url, timeout=None, **kwargs):
        self.requested.append(url)
        for name, payload in self.archives.items():
            if f"/{name}_libretro.so.zip" in url:
                return FakeResponse(payload)
        return FakeResponse(b"", status=404)


# ── The catalogue ─────────────────────────────────────────────────

def test_every_offered_core_covers_at_least_one_system(library, tmp_path):
    for core in retroarch.available_cores(library, directory=tmp_path):
        assert core.systems, core.name


def test_a_core_is_offered_once_however_many_systems_it_runs(library, tmp_path):
    """genesis_plus_gx runs four Sega systems. Listing it four times would
    show the same download four times over."""
    cores = retroarch.available_cores(library, directory=tmp_path)
    names = [core.name for core in cores]

    assert len(names) == len(set(names))

    sega = next(core for core in cores if core.name == "genesis_plus_gx")
    assert len(sega.systems) > 1


def test_systems_with_no_libretro_core_are_left_out(library, tmp_path):
    """The modern consoles have no core, and offering one that cannot exist is
    worse than not offering it."""
    covered = {
        system_id
        for core in retroarch.available_cores(library, directory=tmp_path)
        for system_id in core.system_ids
    }

    for system_id in ("ps3", "switch", "xbox360"):
        assert system_id not in covered


def test_owned_systems_come_first(library, tmp_path):
    for index in range(3):
        library.add_game(title=f"G{index}", system="gba", path=f"/r/{index}.gba")

    cores = retroarch.available_cores(library, directory=tmp_path)

    assert cores[0].name == "mgba"
    assert cores[0].game_count == 3


def test_games_across_a_shared_core_are_counted_together(library, tmp_path):
    library.add_game(title="A", system="megadrive", path="/r/a.md")
    library.add_game(title="B", system="gamegear", path="/r/b.gg")

    cores = retroarch.available_cores(library, directory=tmp_path)
    sega = next(core for core in cores if core.name == "genesis_plus_gx")

    assert sega.game_count == 2


def test_an_installed_core_is_marked_as_such(library, tmp_path):
    (tmp_path / "snes9x_libretro.so").write_bytes(b"x")

    cores = retroarch.available_cores(library, directory=tmp_path)

    assert next(core for core in cores if core.name == "snes9x").installed


def test_only_missing_cores_for_owned_systems_are_suggested(library, tmp_path, monkeypatch):
    library.add_game(title="A", system="snes", path="/r/a.sfc")
    library.add_game(title="B", system="gba", path="/r/b.gba")
    (tmp_path / "snes9x_libretro.so").write_bytes(b"x")
    monkeypatch.setattr(retroarch, "core_directory", lambda **kw: tmp_path)

    assert [core.name for core in retroarch.missing_for_library(library)] == ["mgba"]


# ── Downloading ───────────────────────────────────────────────────

def test_a_core_is_downloaded_and_unpacked(tmp_path):
    session = FakeSession({"snes9x": make_zip()})

    result = retroarch.install_cores(
        ["snes9x"], directory=tmp_path / "cores", session=session
    )

    assert result.installed == ["snes9x"]
    assert (tmp_path / "cores" / "snes9x_libretro.so").is_file()


def test_the_destination_is_created_if_it_does_not_exist(tmp_path):
    """Regression: only the discovered directory was created, so an explicit
    destination failed at the write with an error that read as a download
    problem."""
    session = FakeSession({"snes9x": make_zip()})

    result = retroarch.install_cores(
        ["snes9x"], directory=tmp_path / "deep" / "nested" / "cores", session=session
    )

    assert result.installed == ["snes9x"]


def test_a_core_already_present_is_not_downloaded_again(tmp_path):
    (tmp_path / "snes9x_libretro.so").write_bytes(b"x" * 100)
    session = FakeSession({"snes9x": make_zip()})

    result = retroarch.install_cores(["snes9x"], directory=tmp_path, session=session)

    assert result.skipped == ["snes9x"]
    assert session.requested == []


def test_overwriting_is_available_when_asked_for(tmp_path):
    (tmp_path / "snes9x_libretro.so").write_bytes(b"x" * 100)
    session = FakeSession({"snes9x": make_zip()})

    result = retroarch.install_cores(
        ["snes9x"], directory=tmp_path, session=session, overwrite=True
    )

    assert result.installed == ["snes9x"]


def test_a_core_that_does_not_exist_is_reported(tmp_path):
    session = FakeSession({})

    result = retroarch.install_cores(["nonsense"], directory=tmp_path, session=session)

    assert result.errors
    assert not list(tmp_path.glob("*.so"))


def test_an_implausibly_small_download_is_refused(tmp_path):
    """A saved error page must not be written where RetroArch will try to load
    it — the failure then looks like a broken emulator, not a bad download."""
    session = FakeSession({"snes9x": make_zip(size=10)})

    result = retroarch.install_cores(["snes9x"], directory=tmp_path, session=session)

    assert result.errors
    assert not (tmp_path / "snes9x_libretro.so").exists()


def test_an_archive_with_no_core_in_it_is_refused(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("readme.txt", "not a core")
    session = FakeSession({"snes9x": buffer.getvalue()})

    result = retroarch.install_cores(["snes9x"], directory=tmp_path, session=session)

    assert result.errors


def test_a_corrupt_archive_is_refused(tmp_path):
    session = FakeSession({"snes9x": b"this is not a zip"})

    result = retroarch.install_cores(["snes9x"], directory=tmp_path, session=session)

    assert result.errors


def test_no_partial_file_is_left_behind(tmp_path):
    session = FakeSession({"snes9x": make_zip(size=10)})

    retroarch.install_cores(["snes9x"], directory=tmp_path, session=session)

    assert not list(tmp_path.glob("*.part"))


def test_one_failure_does_not_stop_the_rest(tmp_path):
    session = FakeSession({"snes9x": make_zip(), "mgba": make_zip("mgba_libretro.so")})

    result = retroarch.install_cores(
        ["snes9x", "nonsense", "mgba"], directory=tmp_path, session=session
    )

    assert sorted(result.installed) == ["mgba", "snes9x"]
    assert len(result.errors) == 1


def test_progress_is_reported_per_core(tmp_path):
    session = FakeSession({"snes9x": make_zip(), "mgba": make_zip("mgba_libretro.so")})
    seen = []

    retroarch.install_cores(
        ["snes9x", "mgba"], directory=tmp_path, session=session,
        progress=lambda name, done, total: seen.append((name, done, total)),
    )

    assert seen == [("snes9x", 1, 2), ("mgba", 2, 2)]


def test_downloads_identify_themselves(tmp_path):
    """The buildbot is donated infrastructure."""
    from rose_gamelab.metadata.base import USER_AGENT

    session = FakeSession({"snes9x": make_zip()})
    retroarch.install_cores(["snes9x"], directory=tmp_path, session=session)

    assert session.headers["User-Agent"] == USER_AGENT


# ── Installing RetroArch itself ───────────────────────────────────

def test_gamelab_never_offers_to_run_sudo(monkeypatch):
    """A game launcher acquiring root to install software should not exist."""
    monkeypatch.setattr(retroarch.shutil, "which", lambda _name: None)

    assert not retroarch.can_install_without_root()

    succeeded, message = retroarch.install_retroarch()

    assert not succeeded
    assert "will not ask" in message


def test_the_exact_command_is_given_when_gamelab_cannot_do_it(monkeypatch):
    monkeypatch.setattr(retroarch.shutil, "which", lambda _name: None)

    _succeeded, message = retroarch.install_retroarch()

    assert "pacman" in message or "package manager" in message


def test_an_existing_installation_is_left_alone(monkeypatch):
    monkeypatch.setattr(retroarch, "installed", lambda: True)

    succeeded, message = retroarch.install_retroarch()

    assert succeeded
    assert "already" in message


# ── Where cores go ────────────────────────────────────────────────

def test_a_flatpak_retroarch_gets_its_own_directory(monkeypatch, tmp_path):
    """A Flatpak reads only from inside its sandbox, so an install anywhere
    else downloads perfectly and is then invisible."""
    sandbox = tmp_path / ".var/app/org.libretro.RetroArch/config/retroarch/cores"
    sandbox.mkdir(parents=True)
    ordinary = tmp_path / ".config/retroarch/cores"
    ordinary.mkdir(parents=True)

    monkeypatch.setattr(retroarch, "is_flatpak", lambda: True)
    monkeypatch.setattr(retroarch, "CORE_DIRECTORIES", (
        str(ordinary), str(sandbox),
    ))

    assert retroarch.core_directory() == sandbox


def test_no_directory_is_invented_unless_asked(monkeypatch, tmp_path):
    monkeypatch.setattr(retroarch, "is_flatpak", lambda: False)
    monkeypatch.setattr(retroarch, "CORE_DIRECTORIES", (str(tmp_path / "nope"),))

    assert retroarch.core_directory() is None
    assert retroarch.core_directory(create=True) is not None

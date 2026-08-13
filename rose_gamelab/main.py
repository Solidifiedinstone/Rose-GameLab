"""Entry point for Rose GameLab."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

import click

from rose_gamelab import __version__
from rose_gamelab.db.database import DEFAULT_DB_PATH, Database


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


@click.group(invoke_without_command=True)
@click.option("--database", type=click.Path(), default=None, help="Library database to use.")
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging.")
@click.version_option(__version__, prog_name="Rose GameLab")
@click.pass_context
def main(ctx: click.Context, database: Optional[str], verbose: bool) -> None:
    """Rose GameLab — every game you own, in one place."""
    _configure_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["database"] = Path(database) if database else DEFAULT_DB_PATH

    # Bare `rose-gamelab` opens the interface.
    if ctx.invoked_subcommand is None:
        ctx.invoke(gui)


@main.command()
@click.option("--big-picture", is_flag=True, help="Start in Big Picture mode.")
@click.pass_context
def gui(ctx: click.Context, big_picture: bool = False) -> None:
    """Open the Rose GameLab window."""
    from PySide6.QtWidgets import QApplication

    from rose_gamelab.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Rose GameLab")
    app.setOrganizationName("Rose Open Source Endeavours")
    app.setApplicationVersion(__version__)

    # Ties the window to rose-gamelab.desktop. Without it, Qt derives the
    # Wayland app_id from the process name, so the window reports itself as
    # "python3" — docks then show a generic Python icon, group it with unrelated
    # Python programs, and pinning it pins the interpreter rather than GameLab.
    app.setDesktopFileName("rose-gamelab")

    database = Database(ctx.obj["database"])
    window = MainWindow(database)

    if big_picture:
        window.open_big_picture()
    else:
        window.show()

    exit_code = app.exec()
    database.close()
    sys.exit(exit_code)


@main.command()
@click.argument("folder", type=click.Path(exists=True, file_okay=False))
@click.option("--system", default=None, help="System id, e.g. snes. Inferred when omitted.")
@click.pass_context
def scan(ctx: click.Context, folder: str, system: Optional[str]) -> None:
    """Scan a folder of ROMs into the library."""
    from rich.console import Console

    from rose_gamelab.core.library import Library
    from rose_gamelab.core.scanner import RomScanner

    console = Console()
    database = Database(ctx.obj["database"])
    library = Library(database)
    scanner = RomScanner(library)

    source_id = f"roms:{Path(folder).resolve()}"
    library.register_source(
        source_id, name=Path(folder).name, type="rom_folder",
        path=str(Path(folder).resolve()), system=system,
    )

    result = scanner.scan_folder(folder, system=system, source_id=source_id)

    console.print(
        f"[green]{result.imported.added} added[/], "
        f"{result.imported.updated} updated, "
        f"{result.imported.skipped} already known "
        f"([dim]{result.files_seen} files, {result.games_found} games[/])"
    )
    for error in result.errors:
        console.print(f"[yellow]{error}[/]")

    database.close()


@main.command("import-steam")
@click.pass_context
def import_steam(ctx: click.Context) -> None:
    """Import installed Steam games."""
    from rich.console import Console

    from rose_gamelab.core.library import Library
    from rose_gamelab.sources.steam import SteamProvider

    console = Console()
    database = Database(ctx.obj["database"])
    library = Library(database)

    provider = SteamProvider()
    if not provider.validate():
        console.print("[yellow]No Steam installation found.[/]")
        database.close()
        return

    result = library.import_entries(provider.discover(), source_id="steam")
    console.print(
        f"[green]{result.added} added[/], {result.merged} merged into existing games, "
        f"{result.skipped} already known"
    )
    database.close()


@main.command("find-art")
@click.option("--all", "scrape_all", is_flag=True, help="Include games that already have art.")
@click.pass_context
def find_art(ctx: click.Context, scrape_all: bool) -> None:
    """Download artwork and metadata for library games."""
    from rich.console import Console
    from rich.progress import BarColumn, Progress, TextColumn

    from rose_gamelab.core.library import Library
    from rose_gamelab.metadata.scraper import Scraper

    console = Console()
    database = Database(ctx.obj["database"])
    scraper = Scraper(Library(database))

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
    ) as bar:
        task = bar.add_task("Searching…", total=None)

        def report(state, title):
            bar.update(task, total=state.total, completed=state.processed,
                       description=title[:40])

        state = scraper.scrape_library(only_missing=not scrape_all, progress=report)

    console.print(
        f"[green]Art for {state.art_found}[/], info for {state.metadata_found}, "
        f"nothing found for {state.not_found}"
    )
    for error in state.errors[:10]:
        console.print(f"[yellow]{error}[/]")

    database.close()


@main.command()
@click.argument("game_id", type=int)
@click.pass_context
def play(ctx: click.Context, game_id: int) -> None:
    """Launch a game by its library id, and wait for it to exit.

    This is what the Steam shortcuts created by `export-steam` invoke, so a
    game started from Steam still goes through GameLab's launch profiles and
    playtime tracking. It blocks until the game closes so Steam's own "running"
    state stays accurate.
    """
    from rich.console import Console

    from rose_gamelab.core.controller_profiles import ControllerProfileStore
    from rose_gamelab.core.launcher import Launcher, LaunchError
    from rose_gamelab.core.library import Library
    from rose_gamelab.core.profiles import ProfileStore

    console = Console()
    database = Database(ctx.obj["database"])
    library = Library(database)
    launcher = Launcher(
        library, ProfileStore(database),
        controller_profiles=ControllerProfileStore(database),
    )

    try:
        running = launcher.launch(game_id)
    except LaunchError as exc:
        console.print(f"[red]{exc}[/]")
        database.close()
        sys.exit(1)

    running.wait()
    database.close()


@main.command("export-steam")
@click.option("--collection", default="Rose GameLab", help="Steam library category to file games under.")
@click.option("--system", default=None, help="Only export one system.")
@click.option("--force", is_flag=True, help="Export even if Steam is running (not recommended).")
@click.pass_context
def export_steam(ctx: click.Context, collection: str, system: Optional[str], force: bool) -> None:
    """Add library games to Steam as non-Steam shortcuts, with artwork."""
    from rich.console import Console

    from rose_gamelab.core.library import Library
    from rose_gamelab.sources.steam_export import SteamExporter

    console = Console()
    database = Database(ctx.obj["database"])
    library = Library(database)

    # Exporting Steam games back into Steam would create duplicates of games
    # Steam already has.
    games = [
        game for game in library.list_games(system=system)
        if game.steam_appid is None
    ]

    exporter = SteamExporter()
    try:
        result = exporter.export(games, library, collection_name=collection, force=force)
    except RuntimeError as exc:
        console.print(f"[yellow]{exc}[/]")
        database.close()
        sys.exit(1)

    console.print(
        f"[green]{result.added} added[/], {result.updated} updated, "
        f"{result.artwork_copied} covers copied"
    )
    if result.backup:
        console.print(f"[dim]Previous shortcuts backed up to {result.backup}[/]")
    for error in result.errors:
        console.print(f"[yellow]{error}[/]")

    database.close()


@main.command("list")
@click.option("--system", default=None)
@click.option("--search", default=None)
@click.pass_context
def list_games(ctx: click.Context, system: Optional[str], search: Optional[str]) -> None:
    """List games in the library."""
    from rich.console import Console
    from rich.table import Table

    from rose_gamelab.core.library import Library

    console = Console()
    database = Database(ctx.obj["database"])
    library = Library(database)

    games = library.list_games(system=system, search=search)

    table = Table(show_header=True, header_style="bold")
    table.add_column("Title")
    table.add_column("System")
    table.add_column("Art")
    table.add_column("Playtime", justify="right")

    for game in games:
        table.add_row(
            game.title,
            game.system,
            "yes" if game.has_cover else "—",
            f"{game.playtime_hours} h" if game.play_seconds else "—",
        )

    console.print(table)
    console.print(f"[dim]{len(games)} games[/]")
    database.close()


@main.command("verify")
@click.option("--dats", type=click.Path(), default=None,
              help="Folder of No-Intro/Redump DAT files. Defaults to <data dir>/dats.")
@click.option("--system", default=None, help="Only check one system.")
@click.option("--problems-only", is_flag=True, help="List only files worth acting on.")
@click.pass_context
def verify_roms(
    ctx: click.Context,
    dats: Optional[str],
    system: Optional[str],
    problems_only: bool,
) -> None:
    """Check ROMs against the No-Intro and Redump catalogues.

    DAT files are not shipped with GameLab — the preservation projects publish
    them under their own terms and revise them weekly. Download the sets you
    care about and drop them in the folder.
    """
    from rich.console import Console
    from rich.table import Table

    from rose_gamelab.core import rom_health
    from rose_gamelab.core.library import Library
    from rose_gamelab.db.database import DEFAULT_DB_PATH

    console = Console()
    folder = Path(dats) if dats else DEFAULT_DB_PATH.parent / "dats"

    index = rom_health.load_dats(folder)
    if index.empty:
        console.print(f"[yellow]No usable DAT files in {folder}[/]")
        console.print(
            "Download DATs from [link]https://datomatic.no-intro.org[/] or "
            "[link]http://redump.org/downloads/[/] and put them there."
        )
        return

    console.print(
        f"[dim]{len(index.catalogues)} catalogues loaded: "
        f"{', '.join(index.catalogues)}[/]"
    )

    database = Database(ctx.obj["database"])
    library = Library(database)
    results = rom_health.check_library(library, index, system=system)

    shown = list(rom_health.problems(results)) if problems_only else results

    table = Table(show_header=True, header_style="bold")
    table.add_column("Title")
    table.add_column("System")
    table.add_column("Verdict")

    colours = {
        rom_health.Health.VERIFIED: "green",
        rom_health.Health.KNOWN_BAD: "red",
        rom_health.Health.MODIFIED: "yellow",
        rom_health.Health.UNKNOWN: "dim",
        rom_health.Health.NOT_CATALOGUED: "dim",
    }
    for item in shown:
        colour = colours[item.health]
        table.add_row(item.title, item.system, f"[{colour}]{item.health.label}[/]")

    if shown:
        console.print(table)

    counts = rom_health.summarise(results)
    console.print(
        f"[green]{counts[rom_health.Health.VERIFIED]} verified[/]  "
        f"[red]{counts[rom_health.Health.KNOWN_BAD]} known bad[/]  "
        f"[yellow]{counts[rom_health.Health.MODIFIED]} modified[/]  "
        f"[dim]{counts[rom_health.Health.UNKNOWN]} not catalogued[/]"
    )
    if not results:
        console.print("[dim]Nothing to check — no files have been hashed yet.[/]")

    database.close()


if __name__ == "__main__":
    main()

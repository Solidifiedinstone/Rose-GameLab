"""Entry point for Rose GameLab."""

from __future__ import annotations

import logging
import os
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
    from rose_gamelab.core.system_settings import SystemSettingsStore

    console = Console()
    database = Database(ctx.obj["database"])
    library = Library(database)
    launcher = Launcher(
        library, ProfileStore(database),
        controller_profiles=ControllerProfileStore(database),
        system_settings=SystemSettingsStore(database),
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


@main.command("doctor")
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Print what GameLab can see on this machine.

    Meant to be pasted into a bug report. Everything here is already known
    internally; the point is that a stranger reporting "it will not launch my
    PS2 games" can answer "which emulator did it find" without guessing.

    Prints no paths from inside your home directory beyond the ones GameLab
    itself owns, and never prints a credential.
    """
    import platform
    import shutil
    import sys

    from rich.console import Console
    from rich.table import Table

    from rose_gamelab.core import controller_status, emulator_detect
    from rose_gamelab.core.library import Library
    from rose_gamelab.db.database import DEFAULT_DB_PATH
    from rose_gamelab.ui.preferences import retroachievements_credentials

    console = Console()
    console.print(f"[bold]Rose GameLab {__version__}[/]")
    console.print(
        f"[dim]Python {sys.version.split()[0]} on {platform.system()} "
        f"{platform.release()}[/]"
    )

    session = os.environ.get("XDG_SESSION_TYPE") or "unknown"
    desktop = os.environ.get("XDG_CURRENT_DESKTOP") or "unknown"
    console.print(f"[dim]Session: {session}, desktop: {desktop}[/]\n")

    # ── Library ───────────────────────────────────────────────────
    database = Database(ctx.obj["database"])
    library = Library(database)
    games = library.list_games()
    console.print(f"[bold]Library[/] — schema v{database.version}, {len(games)} games")
    if ctx.obj["database"] and str(ctx.obj["database"]) != str(DEFAULT_DB_PATH):
        console.print("[dim]  (a non-default database was given on the command line)[/]")

    counts = library.systems_in_library()
    if counts:
        console.print(
            "  " + ", ".join(f"{system}: {n}" for system, n in counts[:12])
        )

    # ── Emulators ─────────────────────────────────────────────────
    rows = emulator_detect.summary()
    found = [(sid, name, option) for sid, name, option in rows if option]
    console.print(
        f"\n[bold]Emulators[/] — {len(found)} of {len(rows)} systems playable"
    )

    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("System")
    table.add_column("Emulator")
    table.add_column("How")
    in_library = {system for system, _ in counts}
    for system_id, name, option in rows:
        # Only what the user actually owns games for, plus anything installed.
        if system_id not in in_library and option is None:
            continue
        if option is None:
            table.add_row(name, "[red]none installed[/]", "")
        else:
            table.add_row(name, option.name, option.kind)
    if table.row_count:
        console.print(table)

    # ── Controllers ───────────────────────────────────────────────
    statuses = controller_status.battery_snapshot()
    console.print(f"\n[bold]Controllers[/] — {len(statuses)} device(s)")
    for status in statuses:
        origin = "community database" if status.recognised else "built-in layout"
        console.print(f"  {status.label}  [dim]({status.kind}, {origin})[/]")
    if not statuses:
        console.print("  [dim]nothing connected[/]")

    # ── Optional tools ────────────────────────────────────────────
    # RetroArch is asked about properly rather than looked for on PATH: a
    # Flatpak install puts nothing there, so `which` reported it missing on a
    # machine where it was installed and working.
    retroarch_command = emulator_detect.retroarch_command()
    console.print(
        "\n[bold]RetroArch[/] — "
        + ("[green]" + " ".join(retroarch_command) + "[/]" if retroarch_command
           else "[dim]not installed[/]")
    )
    if retroarch_command:
        from rose_gamelab.core import retroarch as retroarch_module

        cores = retroarch_module.installed_cores()
        console.print(f"  {len(cores)} core(s) installed")

    console.print("\n[bold]Optional tools[/]")
    for tool, what in (
        ("grim", "screenshots (Wayland)"),
        ("spectacle", "screenshots (KDE)"),
        ("maim", "screenshots (X11)"),
        ("gamemoderun", "gamemode"),
        ("mangohud", "MangoHud"),
        ("gamescope", "Gamescope"),
    ):
        mark = "[green]yes[/]" if shutil.which(tool) else "[dim]no[/]"
        console.print(f"  {tool:14} {mark}  [dim]{what}[/]")

    # ── Data ──────────────────────────────────────────────────────
    dats = DEFAULT_DB_PATH.parent / "dats"
    dat_count = len(list(dats.glob("*.dat"))) + len(list(dats.glob("*.xml"))) \
        if dats.is_dir() else 0
    console.print(
        f"\n[bold]Data[/]\n"
        f"  ROM catalogues (DAT files): {dat_count or 'none'}\n"
        f"  RetroAchievements key: "
        f"{'set' if all(retroachievements_credentials()) else 'not set'}"
    )

    database.close()


@main.command("cleanup")
@click.option("--fix", is_flag=True, help="Repair what can be repaired safely.")
@click.pass_context
def cleanup(ctx: click.Context, fix: bool) -> None:
    """Find what has quietly gone wrong in the library.

    Reports by default and changes nothing. `--fix` repairs only the findings
    that need no judgement call — unused cached artwork and empty collections.
    Missing files, duplicates and orphaned games are always left alone: each is
    a decision about your collection, not a tidy-up.
    """
    from rich.console import Console

    from rose_gamelab.core import maintenance
    from rose_gamelab.core.library import Library

    console = Console()
    database = Database(ctx.obj["database"])
    library = Library(database)

    report = maintenance.inspect(library)

    if not report:
        console.print("[green]Nothing wrong found.[/]")
        database.close()
        return

    for finding in report.findings:
        colour = "yellow" if finding.repairable else "red"
        console.print(f"[{colour}]•[/] [bold]{finding.description}[/]")
        console.print(f"  {finding.summary}\n")

    if not fix:
        repairable = report.repairable
        if repairable:
            console.print(
                f"[dim]{len(repairable)} of these can be fixed automatically: "
                "run `rose-gamelab cleanup --fix`.[/]"
            )
        database.close()
        return

    result = maintenance.repair(library, report)
    console.print(f"[green]{result.summary}[/]")
    for error in result.errors:
        console.print(f"[red]  {error}[/]")

    database.close()


@main.group("controllers")
def controllers() -> None:
    """Inspect and share controller mappings."""


@controllers.command("list")
@click.pass_context
def controllers_list(ctx: click.Context) -> None:
    """Show connected pads and the profiles saved for them."""
    from rich.console import Console
    from rich.table import Table

    from rose_gamelab.core import controller_status
    from rose_gamelab.core.controller_profiles import ControllerProfileStore

    console = Console()
    database = Database(ctx.obj["database"])
    store = ControllerProfileStore(database)

    connected = {
        status.device.name for status in controller_status.snapshot()
    }

    table = Table(show_header=True, header_style="bold")
    table.add_column("Player")
    table.add_column("Controller")
    table.add_column("Layout from")
    table.add_column("Now")

    for profile in store.list_profiles():
        table.add_row(
            str(profile.player) if profile.player else "—",
            profile.name,
            "community database" if profile.recognised else profile.source,
            "connected" if profile.device_name in connected else "[dim]not connected[/]",
        )

    if table.row_count:
        console.print(table)
    else:
        console.print("[dim]No controller has been seen yet. Plug one in.[/]")

    database.close()


@controllers.command("export")
@click.argument("path", type=click.Path())
@click.pass_context
def controllers_export(ctx: click.Context, path: str) -> None:
    """Write saved controller mappings to a file others can import."""
    from rich.console import Console

    from rose_gamelab.core.controller_profiles import ControllerProfileStore

    console = Console()
    database = Database(ctx.obj["database"])
    count = ControllerProfileStore(database).export_profiles(path)

    if count:
        console.print(f"[green]Wrote {count} profile(s) to {path}[/]")
    else:
        console.print("[yellow]There are no saved profiles to export yet.[/]")

    database.close()


@controllers.command("import")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option("--overwrite", is_flag=True,
              help="Replace mappings you have already saved for the same pad.")
@click.pass_context
def controllers_import(ctx: click.Context, path: str, overwrite: bool) -> None:
    """Read controller mappings from a file.

    Mappings you already have are kept unless --overwrite is given: importing
    someone else's file should not silently undo your own corrections.
    """
    from rich.console import Console

    from rose_gamelab.core.controller_profiles import ControllerProfileStore

    console = Console()
    database = Database(ctx.obj["database"])
    summary = ControllerProfileStore(database).import_profiles(path, overwrite=overwrite)

    console.print(f"[red]{summary.summary}[/]" if summary.errors
                  else f"[green]{summary.summary}[/]")
    database.close()


@main.command("system")
@click.argument("system_id")
@click.option("--emulator", default=None,
              help="Path to the emulator to use for this system, overriding detection.")
@click.option("--args", "extra_args", default=None,
              help="Arguments appended to every launch on this system.")
@click.option("--clear", is_flag=True, help="Forget the settings for this system.")
@click.pass_context
def system_command(
    ctx: click.Context,
    system_id: str,
    emulator: Optional[str],
    extra_args: Optional[str],
    clear: bool,
) -> None:
    """Show or set how one system is launched.

    With no options it prints what is configured and what detection would pick.

        rose-gamelab system ps2 --args "-fullscreen"
        rose-gamelab system xbox360 --emulator /usr/bin/xenia_canary
    """
    from rich.console import Console

    from rose_gamelab.core import emulator_detect
    from rose_gamelab.core.emulator import get_system
    from rose_gamelab.core.system_settings import SystemSettingsStore

    console = Console()

    if get_system(system_id) is None:
        console.print(f"[red]There is no system called '{system_id}'.[/]")
        console.print("[dim]Try `rose-gamelab doctor` for the systems in use.[/]")
        return

    database = Database(ctx.obj["database"])
    store = SystemSettingsStore(database)

    if clear:
        store.clear(system_id)
        console.print(f"[green]Settings for {system_id} cleared.[/]")
        database.close()
        return

    if emulator is not None or extra_args is not None:
        current = store.get(system_id)
        store.set(
            system_id,
            emulator_path=emulator if emulator is not None else current.emulator_path,
            extra_args=extra_args if extra_args is not None else current.extra_args,
        )

    setting = store.get(system_id)
    detected = emulator_detect.best_for(system_id)

    console.print(f"[bold]{get_system(system_id).name}[/]")
    console.print(
        f"  detected:   {detected.name if detected else '[red]nothing installed[/]'}"
    )
    console.print(f"  configured: {setting.emulator_path or '[dim]use detection[/]'}")
    console.print(f"  arguments:  {setting.extra_args or '[dim]none[/]'}")

    if setting.emulator_path and not Path(setting.emulator_path).exists():
        console.print(
            "[yellow]  That emulator is not there any more; detection will be "
            "used until it comes back.[/]"
        )

    database.close()


@main.command("merge")
@click.argument("keep", type=int)
@click.argument("others", type=int, nargs=-1, required=True)
@click.pass_context
def merge(ctx: click.Context, keep: int, others: tuple[int, ...]) -> None:
    """Fold duplicate entries into one, keeping KEEP.

    Everything the others had moves across: files, ways to play, playtime,
    achievements, collections. Nothing on disk is touched — their files are
    re-pointed at the entry you keep, never deleted.

        rose-gamelab merge 12 47
    """
    from rich.console import Console

    from rose_gamelab.core.curation import merge_games
    from rose_gamelab.core.library import Library

    console = Console()
    database = Database(ctx.obj["database"])
    library = Library(database)

    keeper = library.get(keep)
    if keeper is None:
        console.print(f"[red]There is no game with id {keep}.[/]")
        database.close()
        return

    for game_id in others:
        game = library.get(game_id)
        console.print(
            f"  folding [yellow]{game.title if game else game_id}[/] "
            f"into [green]{keeper.title}[/]"
        )

    result = merge_games(library, keep, others)
    console.print(f"[red]{result.summary}[/]" if result.errors
                  else f"[green]{result.summary}[/]")
    database.close()


@main.command("backup-configs")
@click.option("--label", default=None, help="A name to remember this backup by.")
@click.option("--list", "show", is_flag=True, help="List existing backups instead.")
@click.pass_context
def backup_configs(ctx: click.Context, label: Optional[str], show: bool) -> None:
    """Copy emulator configuration somewhere safe.

    Saves are one game's progress; a configuration is every graphics tweak and
    controller binding you have arrived at over months. Backups are plain files
    you can read and restore with a file manager.
    """
    from rich.console import Console

    from rose_gamelab.core import emulator_configs

    console = Console()

    if show:
        backups = emulator_configs.list_backups()
        if not backups:
            console.print("[dim]No configuration backups yet.[/]")
            return
        for path in backups:
            console.print(f"  {path}")
        return

    found = emulator_configs.known_locations()
    if not found:
        console.print("[yellow]No emulator configuration found on this machine.[/]")
        return

    console.print(
        "[dim]" + ", ".join(sorted({loc.emulator for loc in found})) + "[/]"
    )
    result = emulator_configs.back_up(label=label)

    console.print(f"[green]{result.summary}[/]")
    if result.directory:
        console.print(f"[dim]{result.directory}[/]")
    for error in result.errors[:5]:
        console.print(f"[yellow]  {error}[/]")


if __name__ == "__main__":
    main()

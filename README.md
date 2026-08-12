```
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
                \|
```

# Rose GameLab

**Every game you own, from every source, in one library.**

Rose GameLab is a free and open-source game launcher for Linux. It pulls in your
emulated games, your Steam library, your GOG and Heroic games, and anything else
you point it at — then shows them all together as one clean, themable collection.

Part of **R.O.S.E.** (Rose Open Source Endeavours), a nonprofit building free,
open-source alternatives to big-tech applications.

> **Status: alpha.** The backend is complete and tested; the interface works but
> has had limited real-world use. Features below are marked with what actually
> works today — nothing here is a placeholder pretending to be finished.

## Principles

- **No telemetry.** Nothing is measured, collected, or phoned home. Ever.
- **No accounts.** You never sign up for anything to use GameLab.
- **Works offline.** Metadata and art are cached locally. An internet connection
  makes it nicer; it is never required to browse or launch your games.
- **Your data stays yours.** The library is a plain SQLite file you can read,
  back up, export, or delete.

## Features

**One library**
- Import from emulators, ROM folders, Steam, GOG, Heroic, Lutris, or a custom source
- Sources and systems appear in the sidebar as you add them
- Duplicate detection — the same game owned on Steam *and* dumped as a ROM
  becomes one entry with two ways to play, not two entries
- Multi-disc games merge into a single entry with an auto-generated `.m3u`,
  so the emulator can swap discs without returning to the launcher

**Knowing what your games are**
- Games identified by content hash rather than filename, so renaming a ROM
  never orphans its artwork, playtime or achievements
- Cover art from Steam's CDN and libretro's thumbnail archive; a clean titled
  placeholder when no art exists
- Genre, release date, developer and review scores from Steam and OpenVGDB
- Collections, tags, filters, full-text search, and "surprise me"

**Playing them**
- Per-game launch profiles — Proton version, Gamescope, MangoHud, gamemode,
  environment variables, custom arguments — with a configurable default
- One controller configuration exported to every emulator at once
- Big Picture mode: fullscreen, high-contrast, fully gamepad-navigable
- Playtime tracking. Launches that hand off to another client (Steam, Heroic,
  Lutris) are marked untracked rather than recording a wrong number
- RetroAchievements support (needs your own RetroAchievements API key)

**Managing them**
- Save file and save state management: found where your emulators keep them,
  never moved, backed up as plain folders you can read without GameLab
- Export to Steam as non-Steam shortcuts, with cover art and library categories
- Library import/export so you can move machines or share a curated set

- Rip a CD or DVD to a `.bin`/`.cue` or `.iso` and add it to the library, and
  burn your own DRM-free images back to disc with a read-back verification pass

**Deliberately unfinished**

Some things are left unimplemented rather than guessed, because a confidently
wrong config or format costs you more than none at all:

- Controller exporters exist for RetroArch, SDL, DuckStation and PCSX2. Other
  emulators read gamepads through SDL, so the SDL mapping already covers their
  button layout — see `core/controller.py` for what was verified and how.
- RetroAchievements hashing is implemented for the cartridge systems whose
  algorithm could be verified; the rest raise a named error.
- The rip and burn command lines have never been run against real hardware
  (no optical drive was available), though the parsing, drive detection,
  cancellation and cue conversion are all tested.

## Requirements

- Linux
- Python 3.12+

## Installation

Not yet packaged. Until it ships as a Flatpak and an AUR package:

```sh
git clone https://github.com/Solidifiedinstone/Rose-GameLab
cd Rose-GameLab
python -m venv --system-site-packages .venv
.venv/bin/pip install -e .
.venv/bin/rose-gamelab
```

To get a launcher icon in your application menu or dock:

```sh
./packaging/install-desktop-entry.sh
```

That installs a desktop entry and icons under `~/.local`, needs no root, and
can be undone with `--uninstall`. It writes the launcher's absolute path into
the entry, because desktop files do not inherit your shell's `PATH` — a bare
command name works in a terminal and then silently fails from a dock.

## Command line

GameLab works headlessly too:

```sh
rose-gamelab                      # open the window
rose-gamelab gui --big-picture    # start in Big Picture mode
rose-gamelab import-steam         # import installed Steam games
rose-gamelab scan ~/ROMs/snes --system snes
rose-gamelab find-art             # download covers and metadata
rose-gamelab list --system ps1
rose-gamelab export-steam         # add games to Steam (close Steam first)
```

## Development

```sh
.venv/bin/python -m pytest tests/ -q
```

566 tests, none of which touch the network or require a controller, an optical
drive, or any launcher to be installed.

## Contributing

Contributions are welcome. The one rule that matters: **never merge a feature
that only looks like it works.** If something is unimplemented, it should be
visibly unimplemented rather than silently returning empty results.

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).

GameLab is copyleft on purpose. Anyone may use, study, modify, and redistribute
it, but modified versions distributed to others must also be free software. It
cannot be turned into a closed product.

## Credits

The rose ASCII art is *"rose (3/99)"* by
[Joan G. Stark](https://en.wikipedia.org/wiki/Joan_Stark) ("jgs"), an American
ASCII artist active 1996–2003, from her archived gallery
([github.com/oldcompcz/jgs](https://github.com/oldcompcz/jgs)). Her signature
has been removed from the displayed art; credit is preserved here.

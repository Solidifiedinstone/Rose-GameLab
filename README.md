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

## ⚠️ Vibecoders begone

Yes, this is a vibecoded project. I'm new to programming and still learning
Python - I couldn't have written all of this myself yet, so I leaned on my Local LLM
for assistance to get a working prototype off the ground. This is a starting
point, not a finished, "proper" codebase.

**From here on, I want contributions to be human-written.** If you have
ideas, feature requests, or bug reports, please open an
[Issue](https://github.com/Solidifiedinstone/Rose-GameLab/issues) instead of
sending an AI-generated PR. My goal is to actually learn and own this
codebase as I keep improving at Python, and eventually replace all of it
with code written by hand - mine or a human contributor's.

## Expect bugs

This is early software and it will break on things I have not seen. My own
library is PS2, PS3 and Steam on Arch - your emulators, your distribution and
your dumps are all different, and that is exactly where it will fall over.

**Please post anything that goes wrong in the
[Issues tab](https://github.com/Solidifiedinstone/Rose-GameLab/issues).** Run it
from a terminal and paste what it printed, say which distribution and which
system the game was for, and I will fix it. A bug nobody reports is a bug that
stays.

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
- **Controllers bind themselves.** Plug a pad in and it is recognised against
  the community mapping database — including retro pads on Raphnet, Mayflash
  and 8BitDo adapters — then handed to every SDL-based emulator at once, so
  PCSX2, DuckStation, Dolphin, PPSSPP, RPCS3, Ryujinx and RetroArch all agree
  about which button is which without configuring any of them
- Per-game controller overrides and player order, so the arcade stick drives
  arcade games and you decide who is player one
- Controller mappings export to a file and import from one, so a friend with
  the same pad does not have to map it again
- Per-system emulator choice and arguments: pick which of two installed
  emulators runs a system, and pass it "always fullscreen" once instead of
  per game
- Battery and connection status for pads and wireless peripherals, in both the
  desktop window and Big Picture
- Big Picture mode: fullscreen, high-contrast, keyboard and mouse navigable
- Playtime tracking. Launches that hand off to another client (Steam, Heroic,
  Lutris) are marked untracked rather than recording a wrong number
- RetroAchievements support (needs your own RetroAchievements API key)

**Managing them**
- Save file and save state management: found where your emulators keep them,
  never moved, backed up as plain folders you can read without GameLab
- Export to Steam as non-Steam shortcuts, with cover art and library categories
- Library import/export so you can move machines or share a curated set
- **ROM health check** — verify your dumps against No-Intro and Redump
  catalogues and find the bad ones before they fail three hours in
  (`rose-gamelab verify`; you supply the DAT files)
- An in-game panel on Shift+Tab: screenshots, saves, achievements and pad
  battery over a running game
- **A cleanup pass** — `rose-gamelab cleanup` finds missing files, duplicates,
  games with no emulator installed and artwork nothing refers to. It reports by
  default; `--fix` only ever touches GameLab's own cache, never your games
- **`rose-gamelab doctor`** — one page describing what GameLab can see on this
  machine, meant to be pasted into a bug report
- **RetroArch, set up for you.** Settings → RetroArch installs it (through
  Flatpak, which needs no password) and lists every system with a tick box.
  Tick the consoles you own and GameLab fetches exactly those cores from the
  libretro project — the systems you have games for are ticked already

**Deliberately unfinished**

Some things are left unimplemented rather than guessed, because a confidently
wrong config or format costs you more than none at all:

- Controller exporters exist for RetroArch, SDL, DuckStation and PCSX2. Other
  emulators read gamepads through SDL, so the SDL mapping already covers their
  button layout — see `core/controller.py` for what was verified and how.
- Player order is applied where a format can express it (RetroArch takes an
  explicit joypad index). SDL emulators enumerate pads in kernel order and
  offer no way to reorder them, so for those it is recorded, not faked.
- The in-game panel is a window above the game, not a layer drawn inside it —
  GameLab hooks no renderers. It will not appear in a fullscreen-exclusive
  capture. That is inherent to the approach rather than a bug to be fixed.
- RetroAchievements hashing is implemented for the cartridge systems whose
  algorithm could be verified; the rest raise a named error.

## Installing on Linux

GameLab is not packaged yet — no Flatpak, no AUR package. Until then it installs
with **pipx** in about a minute, and cleanly: pipx gives the application its own
isolated environment, puts one command on your `PATH`, and writes nothing outside
your home directory.

Use pipx rather than `pip` because most distributions now ship a Python that
refuses `pip install` outright (PEP 668, "externally managed environment").
pipx works on those distributions without arguments, and uninstalls just as
cleanly.

### What you need

- **Linux** with a Wayland or X11 session
- **Python 3.12 or newer** — `python --version` to check
- **pipx** — `sudo pacman -S python-pipx`, `sudo apt install pipx`,
  `sudo dnf install pipx`, or `sudo zypper install python3-pipx`
- **Qt 6 system libraries**, which your distribution already ships

Most distributions have everything already. If PySide6 fails to start with a
missing-library error, install Qt's runtime dependencies:

```sh
# Arch, Artix, Manjaro, EndeavourOS
sudo pacman -S --needed python qt6-base qt6-multimedia

# Debian, Ubuntu, Mint, Pop!_OS
sudo apt install python3 python3-venv libgl1 libxkbcommon-x11-0 libegl1

# Fedora
sudo dnf install python3 qt6-qtbase qt6-qtmultimedia

# openSUSE
sudo zypper install python312 libQt6Gui6 libQt6Multimedia6
```

### Install

```sh
pipx install git+https://github.com/Solidifiedinstone/Rose-GameLab
```

That is the whole install. To run it:

```sh
rose-gamelab
```

If your shell cannot find the command, `pipx ensurepath` adds pipx's directory
to your `PATH` — then open a new terminal.

Prefer to keep a copy of the source (you want the desktop entry below, or you
plan to poke at the code)? Clone first and install from the folder:

```sh
git clone https://github.com/Solidifiedinstone/Rose-GameLab
cd Rose-GameLab
pipx install .
```

### Put it in your application menu

From a clone of the repository:

```sh
./packaging/install-desktop-entry.sh
```

This adds a desktop entry and icons under `~/.local`, so GameLab appears in
your launcher and dock like any other application. It needs no root, and
`./packaging/install-desktop-entry.sh --uninstall` removes it again.

It writes the launcher's **absolute path** into the entry on purpose: desktop
files do not inherit your shell's `PATH`, so a bare command name works in a
terminal and then silently fails from a dock. The script finds whichever
`rose-gamelab` your `PATH` has, so a pipx install is picked up automatically.

### First run

1. **Add your games.** Open **Add Source** and point it at a ROM folder, or let
   it find Steam, Heroic, Lutris and GOG installs by itself. Drag a ROM onto the
   window (or press `Ctrl+O`) to file a loose one into place.
2. **Emulators are detected for you** — native binaries, Flatpaks, AppImages and
   RetroArch cores, across 45 systems. Anything missing is named, with the exact
   command to install it.
3. **Art arrives on its own**, from Steam's CDN and the libretro thumbnail
   archive. No API key, no account.
4. **Achievements are optional.** RetroAchievements needs your own free API key
   (Settings → RetroAchievements) because achievements are tied to your account.

Nothing phones home, nothing needs an account, and everything works offline
except art and achievements — which is unavoidable, since both live on the
internet.

### Where your data lives

```
~/.local/share/rose-gamelab/library.db     your library
~/.config/rose-gamelab/preferences.json    theme, style, startup behaviour
~/.config/rose-gamelab/credentials.json    API keys, owner-readable only (0600)
~/.config/rose-gamelab/art/                downloaded covers
```

Deleting those three directories removes GameLab completely. It never touches
your ROMs, saves or game files — only reads them.

### Updating

```sh
pipx upgrade rose-gamelab
```

Installed from a clone instead? `git pull` first, then `pipx install --force .`
from the same folder.

The library database migrates itself forward; your games, collections, notes
and playtime survive.

To remove GameLab entirely: `pipx uninstall rose-gamelab`.

### If something goes wrong

```sh
rose-gamelab --help          # every command
rose-gamelab list            # does it see your games?
```

Run it from a terminal to see what it is doing — errors are printed there rather
than swallowed. Bug reports are welcome, and the more of that output you paste
in, the faster it gets fixed.

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
rose-gamelab doctor               # what GameLab sees here — paste into bug reports
rose-gamelab cleanup              # what has gone wrong in the library
rose-gamelab verify --dats ~/dats # check ROMs against No-Intro/Redump
rose-gamelab controllers list     # pads seen, and the mappings saved for them
rose-gamelab system ps2 --args "-fullscreen"
```

## Development

Working on GameLab rather than just running it? Use a virtualenv with an
editable install — pipx is for installing the application, not for developing it.

```sh
git clone https://github.com/Solidifiedinstone/Rose-GameLab
cd Rose-GameLab
python -m venv --system-site-packages .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests/ -q
```

1238 tests, none of which touch the network or require a controller, an optical
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

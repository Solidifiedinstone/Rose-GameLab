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

> **Status: early development.** The project is being rebuilt from the ground up.
> Features are listed below as *planned* until they actually work — nothing here
> is a placeholder pretending to be finished. See [ROADMAP.md](ROADMAP.md) for
> what is real today.

## Principles

- **No telemetry.** Nothing is measured, collected, or phoned home. Ever.
- **No accounts.** You never sign up for anything to use GameLab.
- **Works offline.** Metadata and art are cached locally. An internet connection
  makes it nicer; it is never required to browse or launch your games.
- **Your data stays yours.** The library is a plain SQLite file you can read,
  back up, export, or delete.

## Planned features

**One library**
- Import from emulators, ROM folders, Steam, GOG, Heroic, or any custom source
- Sources appear in the sidebar as you add them
- Duplicate detection — the same game from three sources becomes one entry with
  three ways to launch it
- Multi-disc games merge into a single entry with an auto-generated `.m3u`

**Knowing what your games are**
- ROM hash matching against No-Intro / Redump datasets, so games are identified
  by content rather than guessed from filenames
- Cover art scraping, with a clean titled placeholder when no art exists
- Genre, release date, and reviews pulled from IGDB and Steam
- Collections, tags, filters, and "surprise me"

**Playing them**
- Per-game launch profiles (Proton version, Gamescope, MangoHud, gamemode,
  environment variables, custom arguments) with a configurable default profile
- One controller configuration applied across *all* emulators at once
- Big Picture mode — a fullscreen, fully gamepad-navigable interface
- Playtime tracking and RetroAchievements support

**Managing them**
- Save file and save state management, cleanly organized and easy to browse
- Library import/export so you can move machines or share a curated set
- Export to Steam as non-Steam shortcuts, with art and library categories
- Burn/rip support for DRM-free discs

## Requirements

- Linux
- Python 3.12+

## Installation

Not yet packaged. Once the rebuild lands, GameLab will ship as a Flatpak and an
AUR package.

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

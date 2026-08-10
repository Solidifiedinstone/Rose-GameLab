# Contributing to Rose GameLab

Contributions are welcome — code, bug reports, emulator configurations,
translations, or just telling us what broke on your machine.

## The one rule that matters

**Never ship something that only looks like it works.**

This project was rebuilt from a codebase where the window opened, every button
was present and styled, and behind it the Steam importer always found zero
games, the art scraper always found zero art, controller detection always
returned an empty list, and the download progress bar was a `time.sleep` loop.
It looked far more finished than it was, and that cost real time.

So:

- **No bare `except: pass`.** If something fails, say what failed and why. An
  empty result and a failure are different things and must be distinguishable
  by the caller.
- **If you cannot verify a format, do not guess it.** Leave it unimplemented
  with a comment naming what you could not determine. A confidently wrong
  emulator config costs the user more than no config at all.
- **No fake progress.** A progress bar must be backed by actual work.
- **Report what happened, not what you hope happened.** Scan and import results
  carry real counts, including zero.

If you are unsure whether something is verifiable, open an issue and ask. "I
could not confirm this" is a completely acceptable answer and is preferred to a
plausible invention.

## Getting set up

```sh
git clone https://github.com/Solidifiedinstone/Rose-GameLab
cd Rose-GameLab
python -m venv --system-site-packages .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests/ -q
```

`--system-site-packages` lets the virtualenv use a system PySide6, which is how
most distributions ship it. If you would rather install it into the venv, drop
that flag.

## Tests

Every test must pass with:

- no network access
- no controller connected
- no optical drive
- no emulator, Steam, Heroic, GOG or Lutris installed

Providers take an injected session so they can be driven by a fake. Anything
touching the filesystem uses `tmp_path`. If a test needs external data, build a
synthetic fixture that matches the real format, and say in a comment where the
real format came from.

Name a regression test after the bug it prevents, and put the reasoning in the
docstring. `test_skips_games_needing_an_update` explains that `StateFlags & 2`
means "update required", not "installed" — that sentence is the point of the
test.

## Style

- Follow the file you are editing. Comment density, naming and structure should
  match its neighbours.
- Comments explain **why**, not what. If a line needs a comment to say what it
  does, rewrite the line.
- Module docstrings explain what the module is for and which decisions are
  load-bearing. Several modules document a format's traps; keep that up.
- `ruff check rose_gamelab tests` should be clean.

## Adding an emulator

Add it to `SYSTEMS` in `rose_gamelab/core/emulator.py` with its real file
extensions. Extensions overlap heavily across systems, which is expected —
content hashing does the actual identification.

If you are adding a controller config exporter, verify the format against a real
config file or the emulator's own source, and say in a comment which one you
checked. Several formats are deliberately unimplemented for exactly this reason.

## Commits

Explain why the change was needed, not just what changed. If you fixed a bug,
describe the failure mode — that is what makes the history useful a year later.

## Licence

Rose GameLab is GPL-3.0-or-later. By contributing you agree your work is
licensed under the same terms. This is deliberate: it keeps the project, and
anything built from it, free software.

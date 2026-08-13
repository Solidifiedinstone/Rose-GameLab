"""RetroAchievements: achievement metadata for emulated games.

RetroAchievements is the only provider here that needs the *user's own*
credentials. That is inherent to what it returns: achievement progress is
per-account, so there is no anonymous form of "which achievements has this
player earned". The user pastes their username and web API key from their RA
profile page (https://retroachievements.org/controlpanel.php) and we read them
from config — never from the database, because an API key is a credential and
the library database is a plain file users copy around, hand to friends, and
open in SQLite browsers. The key is never logged, never written to the
database, and never put in a log message even at debug level.

Two halves live in this file:

1. `ra_hash()` — RetroAchievements identifies a ROM by its own per-console
   hash, not by a plain file checksum. Only the cartridge systems whose
   algorithm we could actually pin down are implemented; everything else
   raises `UnverifiedHashAlgorithm` rather than returning a plausible-looking
   wrong hash. See the RA_HASH section below for the exact split and why.

2. `RetroAchievementsProvider` — the web API client, in the same shape as
   `SteamStoreProvider`: injected session for tests, self-imposed rate limit,
   and a hard distinction between "no such game" (returns None) and "could not
   reach RA" (raises ProviderError), so callers can retry the second and not
   the first.

Rate limiting is self-imposed. RA runs on donated hosting and asks integrators
not to hammer the API; getting GameLab's user agent blocked would break the
feature for everyone, not just the impatient user.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import requests

from rose_gamelab.core.hashing import CHUNK_SIZE, detect_header_size
from rose_gamelab.metadata.base import (
    USER_AGENT,
    GameMetadata,
    MetadataProvider,
    ProviderError,
)

logger = logging.getLogger(__name__)

API_ROOT = "https://retroachievements.org/API"

# Achievement badges live on RA's media host, not the API host. Badge names in
# the API payload are bare ids ("12345"); the "_lock" suffix variant is the
# greyed-out version, which we do not use — the UI dims earned/unearned itself.
BADGE_BASE = "https://media.retroachievements.org/Badge"

# Requests per second budget expressed as a minimum interval, deliberately
# conservative for the same reason as the Steam provider.
RATE_LIMIT = 1.0
REQUEST_TIMEOUT = 20


# ══ RA hashing ════════════════════════════════════════════════════
#
# WHAT IS IMPLEMENTED, AND WHY ONLY THIS MUCH
#
# RetroAchievements' hash is an MD5, but of console-specific *input*, not of
# the file. For plain cartridge dumps the input is the ROM data with any copier
# header removed — which is exactly what `core.hashing.detect_header_size`
# already computes. Those cases are implemented here.
#
# For everything else the input is genuinely involved: N64 requires normalising
# the three byte orders to big-endian first; disc systems (PS1, PS2, PSP,
# Saturn, Dreamcast, Sega CD, PC Engine CD) hash a specific executable or boot
# sector extracted from the filesystem on the disc image, with per-console
# rules about which file and how much of it; arcade hashes the ROM set *name*
# rather than any content. Those algorithms are NOT implemented here because
# they were not verified against a reference implementation while writing this,
# and a hash that is merely plausible is worse than no hash at all: it silently
# matches nothing and looks like "the game has no achievements".
#
# Calling ra_hash() for an unimplemented system raises UnverifiedHashAlgorithm
# naming the system. See UNVERIFIED_SYSTEMS below for the full list.
#
# CAVEAT on the implemented set: these follow the documented cartridge
# behaviour (header-stripped MD5 / whole-file MD5) and are unit-tested here
# against hashes computed the same way, but they have NOT been checked against
# live RetroAchievements server responses from this environment, because these
# tests do not touch the network. The first real ROM that fails to match should
# be treated as a bug in this table, not as "RA doesn't have that game".

#: Systems whose RA hash is MD5 of the ROM data with the copier header
#: stripped. The header detection is shared with No-Intro hashing.
_HEADER_STRIPPED: dict[str, tuple[str, ...]] = {
    # iNES/NES 2.0 dumps carry a 16-byte header that is not ROM data.
    "nes": (".nes",),
    # SNES copier headers are 512 bytes with no magic number; detected by size.
    "snes": (".smc", ".sfc", ".swc", ".fig"),
}

#: Systems whose RA hash is MD5 of the entire file. These media have no copier
#: header in standard dumps, so there is nothing to strip.
_WHOLE_FILE: dict[str, tuple[str, ...]] = {
    "gb": (".gb",),
    "gbc": (".gbc",),
    "megadrive": (".md", ".gen", ".bin"),
    "genesis": (".md", ".gen", ".bin"),
}

#: Systems deliberately left unimplemented, with the reason. Kept as data so
#: the UI can tell the user "RetroAchievements matching is not supported for
#: PlayStation yet" instead of failing mysteriously.
UNVERIFIED_SYSTEMS: dict[str, str] = {
    "n64": "requires normalising .z64/.v64/.n64 byte order before hashing; byte-swap rules not verified",
    "gba": "whole-file MD5 is likely but was not verified; not guessing",
    "nds": "hashes a region of the cartridge header plus arm9/arm7 binaries; not verified",
    "fds": "header handling for Famicom Disk System images not verified",
    "master_system": "not verified",
    "gamegear": "not verified",
    "pc_engine": "not verified",
    "ps1": "hashes the executable named in SYSTEM.CNF on the disc image; not verified",
    "ps2": "hashes the executable named in SYSTEM.CNF on the disc image; not verified",
    "psp": "hashes content from the UMD filesystem; not verified",
    "sega_sat": "disc-based; boot-sector rules not verified",
    "dreamcast": "GD-ROM/CHD track layout handling not verified",
    "segacd": "disc-based; boot-sector rules not verified",
    "arcade": "RA identifies arcade sets by ROM set name, not by file content",
}

#: Interleaved Mega Drive dumps must be de-interleaved before hashing, which is
#: a different algorithm from the whole-file case above. Not implemented.
_INTERLEAVED_MEGADRIVE = (".smd",)


#: RetroAchievements' names for the consoles GameLab can hash for, most likely
#: spelling first. Matched against their own console list rather than mapped to
#: id numbers, because a wrong id silently returns the wrong game's list.
RA_CONSOLE_NAMES: dict[str, tuple[str, ...]] = {
    "nes": ("NES/Famicom", "NES"),
    "snes": ("SNES/Super Famicom", "SNES"),
    "gb": ("Game Boy",),
    "gbc": ("Game Boy Color",),
    "megadrive": ("Genesis/Mega Drive", "Mega Drive", "Genesis"),
    "genesis": ("Genesis/Mega Drive", "Mega Drive", "Genesis"),

    # Consoles RetroAchievements supports but GameLab cannot hash for. They are
    # mapped anyway so a game can be matched by title — see `find_game_by_title`.
    # Without these, a library of PS2 games has no route to achievements at all,
    # which is the common case and was silently unreachable.
    "ps1": ("PlayStation",),
    "psx": ("PlayStation",),
    "ps2": ("PlayStation 2",),
    "psp": ("PlayStation Portable",),
    "arcade": ("Arcade",),
    "dreamcast": ("Dreamcast",),
    "sega_sat": ("Saturn",),
    "segacd": ("Sega CD",),
    "n64": ("Nintendo 64",),
    "gba": ("Game Boy Advance",),
    "nds": ("Nintendo DS",),
    "gamegear": ("Game Gear",),
    "master_system": ("Master System",),
    "pc_engine": ("PC Engine/TurboGrafx-16", "PC Engine"),
    "fds": ("Famicom Disk System",),
}

#: Systems RetroAchievements has no set for at all. Kept as data so the
#: interface can say "PlayStation 3 is not on RetroAchievements" rather than
#: offering a Refresh that can only ever come back empty.
NOT_ON_RETROACHIEVEMENTS: dict[str, str] = {
    "ps3": "RetroAchievements has no PlayStation 3 sets",
    "ps4": "RetroAchievements has no PlayStation 4 sets",
    "pc": "RetroAchievements does not cover Windows games",
    "steam": "Steam games have Steam achievements, not RetroAchievements",
    "wiiu": "RetroAchievements has no Wii U sets",
    "switch": "RetroAchievements has no Switch sets",
    "xbox": "RetroAchievements has no Xbox sets",
    "xenia": "RetroAchievements has no Xbox 360 sets",
}


def on_retroachievements(system: str) -> bool:
    """Whether RetroAchievements covers this system at all."""
    return system not in NOT_ON_RETROACHIEVEMENTS and system in RA_CONSOLE_NAMES


def normalise_title(title: str) -> str:
    """A title reduced to what two databases are likely to agree on.

    Region tags, disc numbers, dump markers, subtitles after a dash, articles
    and punctuation all differ between a ROM filename and RetroAchievements'
    own naming, and none of them identify the game.
    """
    import re as _re

    text = title.lower()
    text = _re.sub(r"\((?:disc|disk|cd)\s*\d+[^)]*\)", " ", text)
    text = _re.sub(r"[\(\[][^)\]]*[\)\]]", " ", text)      # (USA), [!], (Rev 1)
    text = text.replace("&", " and ")
    text = _re.sub(r"[^a-z0-9]+", " ", text)
    text = _re.sub(r"\b(the|a|an)\b", " ", text)
    return " ".join(text.split())


class UnverifiedHashAlgorithm(NotImplementedError):
    """Raised for a system whose RA hash algorithm we did not verify.

    Deliberately not a ProviderError: this is not a transient failure the
    caller should retry, it is a feature that does not exist yet.
    """


def supports_hashing(system: str) -> bool:
    """Whether `ra_hash` can produce a real RA hash for this system."""
    key = system.lower()
    return key in _HEADER_STRIPPED or key in _WHOLE_FILE


def ra_hash(path: str | Path, system: str) -> str:
    """Compute the RetroAchievements hash for a ROM.

    Raises:
        UnverifiedHashAlgorithm: the system's algorithm is not implemented.
        ValueError: the file extension does not match the system, so we cannot
            tell whether header stripping applies. Archives (.zip/.7z) hit this
            too — RA hashes the file *inside* the archive and the extraction
            rules were not verified.
        OSError: the file could not be read.
    """
    path = Path(path)
    key = system.lower()
    suffix = path.suffix.lower()

    if key in ("megadrive", "genesis") and suffix in _INTERLEAVED_MEGADRIVE:
        raise UnverifiedHashAlgorithm(
            "megadrive: interleaved .smd dumps must be de-interleaved before "
            "hashing; that transform was not verified"
        )

    if key in _HEADER_STRIPPED:
        allowed = _HEADER_STRIPPED[key]
        strip_header = True
    elif key in _WHOLE_FILE:
        allowed = _WHOLE_FILE[key]
        strip_header = False
    else:
        reason = UNVERIFIED_SYSTEMS.get(key, "algorithm not verified")
        raise UnverifiedHashAlgorithm(f"{system}: {reason}")

    if suffix not in allowed:
        raise ValueError(
            f"{path.name}: extension {suffix or '(none)'} is not a recognised "
            f"{system} ROM ({', '.join(allowed)}); refusing to guess whether a "
            f"copier header is present"
        )

    digest = hashlib.md5()
    first = True

    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            if first:
                first = False
                if strip_header:
                    # NOTE: for SNES this reuses No-Intro's header test
                    # (size % 1024 == 512). RA's own check is a size mask too;
                    # for real dumps, whose ROM data is a multiple of 32 KiB,
                    # the two agree. A homebrew ROM with an unusual size could
                    # in principle disagree — if one turns up, this is the line
                    # to revisit.
                    chunk = chunk[detect_header_size(path, chunk):]
            digest.update(chunk)

    return digest.hexdigest()


# ══ Web API ═══════════════════════════════════════════════════════

#: RA console ids, for the systems we can hash. Taken from RA's published
#: console list. Only the ids we actually use are listed — an id we guessed
#: would silently query the wrong console.
CONSOLE_IDS: dict[str, int] = {
    "megadrive": 1,
    "genesis": 1,
    "snes": 3,
    "gb": 4,
    "gbc": 6,
    "nes": 7,
}


@dataclass(frozen=True)
class Achievement:
    """One achievement, with this user's progress on it.

    `earned_at` is None when the user has not earned it. `hardcore` means it
    was earned with savestates and cheats disabled, which RA tracks as a
    separate, stricter award.
    """

    ra_id: int
    title: str
    description: Optional[str]
    points: int
    badge_url: Optional[str]
    earned_at: Optional[str] = None
    hardcore: bool = False

    @property
    def earned(self) -> bool:
        return self.earned_at is not None


class RateLimiter:
    """Spaces requests out across threads."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last = time.monotonic()


def credentials_from_config(config) -> tuple[Optional[str], Optional[str]]:
    """Read (username, api_key) out of the GameLab config.

    Credentials live in config (a YAML file the user owns) rather than in the
    library database, under `retroachievements.username` / `.api_key`. Returns
    (None, None) when the user has not set them up, which is the normal state.
    """
    if config is not None:
        username = config.get("retroachievements.username")
        api_key = config.get("retroachievements.api_key")
        if username and api_key:
            return (username, api_key)

    # Where the Settings screen puts them. Imported late so the metadata layer
    # does not depend on the interface.
    try:
        from rose_gamelab.ui.preferences import retroachievements_credentials
        return retroachievements_credentials()
    except Exception:
        return (None, None)


class RetroAchievementsProvider(MetadataProvider):
    """Achievement data from RetroAchievements. Requires the user's API key."""

    name = "RetroAchievements"
    requires_key = True

    def __init__(
        self,
        username: Optional[str] = None,
        api_key: Optional[str] = None,
        *,
        session: Optional[requests.Session] = None,
        rate_limit: float = RATE_LIMIT,
    ) -> None:
        self.username = username or None
        # Stored on the instance only. Never logged, never persisted here.
        self._api_key = api_key or None

        self.session = session or requests.Session()
        # ASSIGNED, not setdefault — requests fills in its own User-Agent when
        # the session is constructed, so setdefault always lost and this
        # provider was identifying itself as python-requests to an API that
        # asks callers to say who they are. The shared constant, so the version
        # is right and there is one copy of it.
        self.session.headers["User-Agent"] = USER_AGENT
        # Tests pass rate_limit=0 to run against fakes without sleeping.
        self.limiter = RateLimiter(rate_limit)
        #: RA console name -> id, fetched once. None until asked for.
        self._consoles: Optional[dict[str, int]] = None

    @classmethod
    def from_config(cls, config, **kwargs) -> "RetroAchievementsProvider":
        username, api_key = credentials_from_config(config)
        return cls(username, api_key, **kwargs)

    def available(self) -> bool:
        """False until the user has supplied both a username and an API key.

        Both are required: `z` identifies the calling account and `y`
        authenticates it, and RA rejects the request if either is missing.
        """
        return bool(self.username and self._api_key)

    # ── Transport ─────────────────────────────────────────────────

    def _get(self, endpoint: str, params: dict[str, Any]) -> Any:
        """Call one API endpoint and return the decoded payload.

        Raises ProviderError for anything that is not a well-formed response —
        including a missing key, since "you are not configured" is a different
        thing from "that game does not exist" and callers must not confuse the
        two. The error message never contains the API key.
        """
        if not self.available():
            raise ProviderError(
                "RetroAchievements is not configured: set "
                "retroachievements.username and retroachievements.api_key in "
                "the GameLab config"
            )

        self.limiter.wait()

        query = {"z": self.username, "y": self._api_key}
        query.update({k: v for k, v in params.items() if v is not None})

        try:
            response = self.session.get(
                f"{API_ROOT}/{endpoint}",
                params=query,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            # str(exc) can embed the request URL, which carries `y`. Report the
            # endpoint and exception type only, so the key never reaches a log.
            raise ProviderError(
                f"could not reach RetroAchievements ({endpoint}): "
                f"{type(exc).__name__}"
            ) from exc
        except ValueError as exc:
            raise ProviderError(
                f"RetroAchievements returned malformed JSON from {endpoint}: {exc}"
            ) from exc

    # ── Game progress ─────────────────────────────────────────────

    def _game_progress(self, ra_game_id: int, user: Optional[str] = None) -> Optional[dict]:
        """Raw GetGameInfoAndUserProgress payload, or None if RA has no such game.

        RA answers an unknown game id with a payload whose ID is null (and, in
        some versions, with an empty list), rather than with a 404 — so a
        falsy ID is the "no such game" signal.
        """
        payload = self._get(
            "API_GetGameInfoAndUserProgress.php",
            {"g": int(ra_game_id), "u": user or self.username},
        )

        if not isinstance(payload, dict) or not payload.get("ID"):
            return None

        return payload

    def fetch(self, ra_game_id: int, user: Optional[str] = None) -> Optional[GameMetadata]:
        """Metadata for an RA game id. None when RA has no such game.

        Raises ProviderError on network failure so the caller can distinguish
        "not on RetroAchievements" from "could not reach RetroAchievements".
        """
        payload = self._game_progress(ra_game_id, user)
        if payload is None:
            return None

        genre = payload.get("Genre")

        return GameMetadata(
            title=payload.get("Title") or None,
            # RA has no free-text summary field; leaving it None lets a better
            # provider fill it in during the merge rather than shadowing it.
            summary=None,
            release_date=_parse_released(payload.get("Released")),
            developer=payload.get("Developer") or None,
            publisher=payload.get("Publisher") or None,
            genres=[g.strip() for g in genre.split(",")] if isinstance(genre, str) and genre else [],
            # RA has no rating of any kind. Deliberately left None rather than
            # inventing one from completion percentage.
            rating=None,
            rating_source=None,
            source="retroachievements",
        )

    def achievements(self, ra_game_id: int, user: Optional[str] = None) -> list[Achievement]:
        """This user's achievement list and progress for one game.

        Returns an empty list both when RA has no such game and when the game
        exists but has no achievements set yet — those are the same thing from
        the caller's point of view. Use `fetch` if you need to tell them apart.
        """
        payload = self._game_progress(ra_game_id, user)
        if payload is None:
            return []

        raw = payload.get("Achievements")
        if not isinstance(raw, dict):
            # RA returns an empty JSON array (not object) for games with no
            # achievement set, which decodes to a list. Nothing to report.
            return []

        return [
            parsed
            for entry in raw.values()
            if (parsed := _parse_achievement(entry)) is not None
        ]

    # ── Account ───────────────────────────────────────────────────

    def completed_games(self, user: Optional[str] = None) -> list[dict]:
        """Games this user has made progress on, as RA reports them.

        Returned verbatim rather than mapped onto a dataclass: the caller uses
        this to cross-reference against the local library, and RA's field set
        here differs from the per-game endpoint's.
        """
        payload = self._get("API_GetUserCompletedGames.php", {"u": user or self.username})
        return payload if isinstance(payload, list) else []

    # ── Hash matching ─────────────────────────────────────────────

    def game_list(self, console_id: int, *, only_with_achievements: bool = True,
                  with_hashes: bool = True) -> list[dict]:
        """Every RA game for one console.

        `with_hashes` asks RA to include the list of ROM hashes that map to
        each game, which is what makes local hash matching possible without a
        per-file lookup request.
        """
        payload = self._get(
            "API_GetGameList.php",
            {
                "i": int(console_id),
                "f": 1 if only_with_achievements else 0,
                "h": 1 if with_hashes else 0,
            },
        )
        return payload if isinstance(payload, list) else []

    def console_id_for(self, system: str) -> Optional[int]:
        """RetroAchievements' own console id for a GameLab system.

        Fetched from RA rather than hardcoded. Their ids are stable but this
        module's rule is that anything not verified against the real service is
        not written down as fact, and there is no way to check a number from
        here. One request, cached for the life of the provider.
        """
        names = RA_CONSOLE_NAMES.get(system)
        if not names:
            return None

        if self._consoles is None:
            try:
                payload = self._get("API_GetConsoleIDs.php", {})
            except ProviderError as exc:
                logger.debug("could not fetch RA console ids: %s", exc)
                return None

            self._consoles = {}
            if isinstance(payload, list):
                for entry in payload:
                    if not isinstance(entry, dict):
                        continue
                    name = str(entry.get("Name") or "").strip().lower()
                    identifier = _as_int(entry.get("ID"))
                    if name and identifier is not None:
                        self._consoles[name] = identifier

        for name in names:
            found = self._consoles.get(name.lower())
            if found is not None:
                return found

        return None

    def find_game_by_title(self, console_id: int, title: str,
                           *, cutoff: float = 0.86) -> Optional[int]:
        """Find a game on a console by name, when its file cannot be hashed.

        A hash is an identity; a title is a guess, so the bar is set high and a
        near-miss is refused rather than returned. Getting the wrong game's
        achievements would be worse than getting none: it would show progress
        for something the user has never played.
        """
        import difflib

        wanted = normalise_title(title)
        if not wanted:
            return None

        candidates = self.game_list(console_id, only_with_achievements=True)
        best_id, best_score = None, 0.0

        for entry in candidates:
            name = entry.get("Title") or entry.get("title") or ""
            score = difflib.SequenceMatcher(None, wanted, normalise_title(name)).ratio()
            if score > best_score:
                best_id, best_score = entry.get("ID") or entry.get("id"), score

        if best_id is None or best_score < cutoff:
            return None
        return int(best_id)

    def find_game_by_hash(self, console_id: int, rom_hash: str) -> Optional[int]:
        """RA game id for a ROM hash, or None if that hash is not in RA's set.

        This pulls the console's whole game list, which is a large response —
        callers matching more than one ROM for the same console should call
        `game_list` once themselves and index it.

        Raises ProviderError if the response carries no hashes at all, because
        that means the request silently came back without `h=1` data and
        answering None would look like "your ROM is unknown to RA" when in
        fact we never checked.
        """
        wanted = rom_hash.lower()
        saw_any_hashes = False

        for entry in self.game_list(console_id):
            hashes = entry.get("Hashes")
            if not isinstance(hashes, Iterable) or isinstance(hashes, (str, bytes)):
                continue
            hashes = list(hashes)
            if hashes:
                saw_any_hashes = True
            if any(str(h).lower() == wanted for h in hashes):
                game_id = entry.get("ID")
                return int(game_id) if game_id is not None else None

        if not saw_any_hashes:
            raise ProviderError(
                f"RetroAchievements game list for console {console_id} carried "
                "no ROM hashes; cannot say whether this ROM is known"
            )

        return None


# ── Persistence helpers ───────────────────────────────────────────

def link_game(db, game_id: int, ra_game_id: Optional[int], rom_hash: Optional[str]) -> None:
    """Record which RA game a library game is, and the hash that matched it."""
    db.execute(
        "UPDATE games SET ra_game_id = ?, ra_hash = ? WHERE id = ?",
        (ra_game_id, rom_hash, game_id),
    )


def save_achievements(db, game_id: int, achievements: Iterable[Achievement]) -> int:
    """Replace the stored achievements for one game. Returns the row count.

    An upsert rather than delete-then-insert so that a failed refresh leaves
    the previous state intact instead of wiping the user's visible progress.
    """
    rows = list(achievements)
    if not rows:
        return 0

    with db.transaction() as cur:
        for achievement in rows:
            cur.execute(
                """
                INSERT INTO achievements
                    (game_id, ra_id, title, description, points, badge_url,
                     earned_at, hardcore)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(game_id, ra_id) DO UPDATE SET
                    title       = excluded.title,
                    description = excluded.description,
                    points      = excluded.points,
                    badge_url   = excluded.badge_url,
                    earned_at   = excluded.earned_at,
                    hardcore    = excluded.hardcore
                """,
                (
                    game_id,
                    achievement.ra_id,
                    achievement.title,
                    achievement.description,
                    achievement.points,
                    achievement.badge_url,
                    achievement.earned_at,
                    1 if achievement.hardcore else 0,
                ),
            )

    return len(rows)


def achievements_for(db, game_id: int) -> list[Achievement]:
    """Every stored achievement for one game, earned ones first.

    Read from the database rather than the network so the page opens instantly
    and works offline — a refresh is something the user asks for, not something
    that happens because they clicked a game.
    """
    rows = db.query(
        """
        SELECT ra_id, title, description, points, badge_url, earned_at, hardcore
          FROM achievements
         WHERE game_id = ?
         ORDER BY earned_at IS NULL, points DESC, title
        """,
        (game_id,),
    )

    return [
        Achievement(
            ra_id=row["ra_id"],
            title=row["title"],
            description=row["description"],
            points=row["points"],
            badge_url=row["badge_url"],
            earned_at=row["earned_at"],
            hardcore=bool(row["hardcore"]),
        )
        for row in rows
    ]


def progress_for(db, game_id: int) -> tuple[int, int, int, int]:
    """(earned, total, points earned, points available) for one game."""
    found = achievements_for(db, game_id)
    earned = [a for a in found if a.earned]
    return (
        len(earned),
        len(found),
        sum(a.points for a in earned),
        sum(a.points for a in found),
    )


# ── Parsing ───────────────────────────────────────────────────────

def _parse_achievement(entry: Any) -> Optional[Achievement]:
    """Build an Achievement from one RA payload entry, or None if unusable.

    An entry without an id or title is not something we can display or store,
    so it is dropped rather than saved as a blank row.
    """
    if not isinstance(entry, dict):
        return None

    ra_id = _as_int(entry.get("ID"))
    title = entry.get("Title")
    if ra_id is None or not title:
        return None

    badge = entry.get("BadgeName")
    # DateEarnedHardcore is present only when the hardcore award was earned;
    # its presence is the flag, and it takes precedence as the earn time
    # because a hardcore award implies the softcore one.
    hardcore_at = entry.get("DateEarnedHardcore") or None
    earned_at = hardcore_at or entry.get("DateEarned") or None

    return Achievement(
        ra_id=ra_id,
        title=str(title),
        description=entry.get("Description") or None,
        points=_as_int(entry.get("Points")) or 0,
        badge_url=f"{BADGE_BASE}/{badge}.png" if badge else None,
        earned_at=str(earned_at) if earned_at else None,
        hardcore=bool(hardcore_at),
    )


def _as_int(value: Any) -> Optional[int]:
    """RA returns numbers as JSON strings in some fields and ints in others."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    if isinstance(value, float):
        return int(value)
    return None


def _parse_released(text: Any) -> Optional[str]:
    """Normalise RA's release date to ISO 8601.

    RA's `Released` is free text and varies by how the entry was filled in:
    '1992-10-21', '1992-10-21 00:00:00', 'October 21, 1992' and bare '1992'
    all occur. Anything that does not parse yields None rather than a wrong
    date — a missing release year is recoverable, a wrong one is not.
    """
    if not isinstance(text, str):
        return None

    text = text.strip()
    if not text:
        return None

    from datetime import datetime

    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"):
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        return parsed.date().isoformat()

    if len(text) == 4 and text.isdigit():
        return text

    return None

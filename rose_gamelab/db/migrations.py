"""Schema migrations for the Rose GameLab library database.

Every schema change is an append-only entry in MIGRATIONS. Never edit a
migration that has shipped — add a new one. `Database` applies any migration
whose version is above the database's current `user_version`, inside a single
transaction, so an interrupted upgrade leaves the file untouched rather than
half-migrated.

Design notes that matter for the rest of the codebase:

- A *game* is the thing you see in the library. A *game_file* is a file on
  disk. They are separate tables so a multi-disc game is ONE game with three
  files, and so a game can survive its files moving or going missing.
- A *launch_option* is one way to start a game. The same game may be launchable
  as a ROM, as a Steam app, and through Heroic; duplicate detection merges
  those into one game with three launch options rather than three entries.
- Files are identified by content hash, not path, so renaming a ROM does not
  orphan its artwork, playtime, or achievements.
"""

from __future__ import annotations

# Each entry: (version, description, SQL). Applied in ascending order.
MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "initial library schema",
        """
        -- ── Sources: where games come from ──────────────────────────
        CREATE TABLE sources (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            type        TEXT NOT NULL,          -- rom_folder | steam | gog | heroic | custom
            path        TEXT,                   -- root path, if applicable
            system      TEXT,                   -- system id for single-system ROM folders
            enabled     INTEGER NOT NULL DEFAULT 1,
            config      TEXT NOT NULL DEFAULT '{}',   -- JSON, source-type specific
            added_at    TEXT NOT NULL,
            scanned_at  TEXT
        );

        -- ── Games: one row per game as the user thinks of it ────────
        CREATE TABLE games (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            title         TEXT NOT NULL,
            sort_title    TEXT NOT NULL,        -- title with leading articles stripped
            system        TEXT NOT NULL,        -- system id, or 'pc'
            source_id     TEXT REFERENCES sources(id) ON DELETE SET NULL,

            -- Metadata (populated by scrapers; all nullable, all optional)
            igdb_id       INTEGER,
            steam_appid   INTEGER,
            summary       TEXT,
            release_date  TEXT,                 -- ISO 8601 date
            developer     TEXT,
            publisher     TEXT,
            rating        REAL,                 -- normalised 0-100
            rating_count  INTEGER,
            rating_source TEXT,                 -- 'igdb' | 'steam'

            -- Local state
            cover_path    TEXT,                 -- path in the art cache
            hero_path     TEXT,
            logo_path     TEXT,
            hidden        INTEGER NOT NULL DEFAULT 0,
            favorite      INTEGER NOT NULL DEFAULT 0,
            added_at      TEXT NOT NULL,
            last_played   TEXT,
            play_seconds  INTEGER NOT NULL DEFAULT 0,
            play_count    INTEGER NOT NULL DEFAULT 0,

            metadata_locked INTEGER NOT NULL DEFAULT 0  -- user edited; scrapers must not overwrite
        );

        CREATE INDEX idx_games_sort   ON games(sort_title);
        CREATE INDEX idx_games_system ON games(system);
        CREATE INDEX idx_games_source ON games(source_id);

        -- ── Files: the actual things on disk ────────────────────────
        CREATE TABLE game_files (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            path        TEXT NOT NULL UNIQUE,
            size_bytes  INTEGER,
            disc_number INTEGER,                -- NULL for single-disc/non-disc games
            disc_label  TEXT,                   -- e.g. 'Disc 1', 'Bonus Disc'

            -- Content hashes for No-Intro / Redump matching. Computed lazily.
            crc32       TEXT,
            md5         TEXT,
            sha1        TEXT,
            hashed_at   TEXT,

            missing     INTEGER NOT NULL DEFAULT 0,   -- file was gone at last scan
            added_at    TEXT NOT NULL
        );

        CREATE INDEX idx_files_game  ON game_files(game_id);
        CREATE INDEX idx_files_sha1  ON game_files(sha1);
        CREATE INDEX idx_files_crc32 ON game_files(crc32);

        -- ── Launch profiles: how a game is run ──────────────────────
        CREATE TABLE launch_profiles (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT NOT NULL UNIQUE,
            is_default     INTEGER NOT NULL DEFAULT 0,
            proton_version TEXT,
            use_gamemode   INTEGER NOT NULL DEFAULT 0,
            use_mangohud   INTEGER NOT NULL DEFAULT 0,
            use_gamescope  INTEGER NOT NULL DEFAULT 0,
            gamescope_args TEXT,
            env            TEXT NOT NULL DEFAULT '{}',  -- JSON dict
            extra_args     TEXT,
            pre_launch     TEXT,                        -- shell command
            post_exit      TEXT
        );

        -- ── Launch options: one per way to start a game ─────────────
        CREATE TABLE launch_options (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            kind        TEXT NOT NULL,          -- emulator | native | steam | heroic | gog | custom
            label       TEXT,                   -- shown when a game has several
            emulator    TEXT,                   -- emulator/core id, for kind='emulator'
            target      TEXT NOT NULL,          -- exe path, steam appid, m3u path, ...
            args        TEXT,
            working_dir TEXT,
            profile_id  INTEGER REFERENCES launch_profiles(id) ON DELETE SET NULL,
            is_primary  INTEGER NOT NULL DEFAULT 0,
            sort_order  INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX idx_launch_game ON launch_options(game_id);

        -- ── Play history ────────────────────────────────────────────
        CREATE TABLE play_sessions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id          INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            launch_option_id INTEGER REFERENCES launch_options(id) ON DELETE SET NULL,
            started_at       TEXT NOT NULL,
            ended_at         TEXT,
            seconds          INTEGER
        );

        CREATE INDEX idx_sessions_game ON play_sessions(game_id);

        -- ── Collections and tags ────────────────────────────────────
        CREATE TABLE collections (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL UNIQUE,
            icon       TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            -- A smart collection stores a filter query and has no explicit members.
            smart_query TEXT
        );

        CREATE TABLE collection_games (
            collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
            game_id       INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            sort_order    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (collection_id, game_id)
        );

        CREATE TABLE tags (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            name   TEXT NOT NULL UNIQUE,
            kind   TEXT NOT NULL DEFAULT 'user',  -- user | genre | mode | perspective
            color  TEXT
        );

        CREATE TABLE game_tags (
            game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            tag_id  INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            PRIMARY KEY (game_id, tag_id)
        );

        CREATE INDEX idx_game_tags_tag ON game_tags(tag_id);

        -- ── Saves and save states ───────────────────────────────────
        CREATE TABLE saves (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id      INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            kind         TEXT NOT NULL,         -- save | state
            slot         INTEGER,               -- state slot number, if any
            path         TEXT NOT NULL,
            size_bytes   INTEGER,
            modified_at  TEXT NOT NULL,
            screenshot   TEXT,                  -- path to state thumbnail
            label        TEXT,                  -- user-supplied name
            backed_up_at TEXT
        );

        CREATE INDEX idx_saves_game ON saves(game_id);

        -- ── Full-text search over titles ────────────────────────────
        CREATE VIRTUAL TABLE games_fts USING fts5(
            title,
            content='games',
            content_rowid='id',
            tokenize='unicode61'
        );

        CREATE TRIGGER games_fts_insert AFTER INSERT ON games BEGIN
            INSERT INTO games_fts(rowid, title) VALUES (new.id, new.title);
        END;

        CREATE TRIGGER games_fts_delete AFTER DELETE ON games BEGIN
            INSERT INTO games_fts(games_fts, rowid, title)
            VALUES ('delete', old.id, old.title);
        END;

        CREATE TRIGGER games_fts_update AFTER UPDATE OF title ON games BEGIN
            INSERT INTO games_fts(games_fts, rowid, title)
            VALUES ('delete', old.id, old.title);
            INSERT INTO games_fts(rowid, title) VALUES (new.id, new.title);
        END;
        """,
    ),
    (
        2,
        "retroachievements: achievements table and RA identity on games",
        """
        -- ── Achievements ────────────────────────────────────────────
        -- One row per achievement per game, carrying this user's progress.
        -- Achievement definitions and progress are stored together because
        -- GameLab is single-user: there is no second player whose progress
        -- would need a separate row against the same definition.
        --
        -- `earned_at` NULL means "not earned" — that is the only unearned
        -- marker, so there is no separate boolean to keep in sync with it.
        -- `hardcore` records that the award was earned with savestates and
        -- cheats disabled, which RetroAchievements tracks as a stricter,
        -- separate award rather than as a different achievement.
        --
        -- No credentials live here. The RA username and API key are read
        -- from the config file; the database is a plain file users copy
        -- around and must never carry a key.
        CREATE TABLE achievements (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            ra_id       INTEGER NOT NULL,       -- RetroAchievements achievement id
            title       TEXT NOT NULL,
            description TEXT,
            points      INTEGER NOT NULL DEFAULT 0,
            badge_url   TEXT,
            earned_at   TEXT,                   -- ISO 8601, NULL when unearned
            hardcore    INTEGER NOT NULL DEFAULT 0,

            -- Lets a refresh upsert instead of duplicating the whole set.
            UNIQUE (game_id, ra_id)
        );

        CREATE INDEX idx_achievements_game ON achievements(game_id);

        -- ── RA identity on games ────────────────────────────────────
        -- `ra_hash` is RetroAchievements' own per-console hash, NOT any of
        -- the checksums in game_files: RA hashes console-specific ROM data,
        -- so the two are different values for the same file and must not be
        -- confused. Stored so a re-match does not need to re-read the ROM.
        ALTER TABLE games ADD COLUMN ra_game_id INTEGER;
        ALTER TABLE games ADD COLUMN ra_hash    TEXT;

        CREATE INDEX idx_games_ra_id   ON games(ra_game_id);
        CREATE INDEX idx_games_ra_hash ON games(ra_hash);
        """,
    ),
    (
        3,
        "per-game notes, and locking artwork separately from metadata",
        """
        -- Whatever the user wants to remember about a game: which save slot
        -- is the good one, that it needs a controller unplugged to boot, where
        -- they got to. Free text, theirs, never written by a scraper.
        ALTER TABLE games ADD COLUMN notes TEXT;

        -- Art chosen by hand must survive a rescrape. `metadata_locked` was
        -- doing that job and doing too much with it: it gates the WHOLE scrape,
        -- so picking a cover also stopped the game from ever getting a
        -- description or a release date. Artwork gets its own lock.
        ALTER TABLE games ADD COLUMN cover_locked INTEGER NOT NULL DEFAULT 0;

        -- Anyone who already chose a cover meant to protect that cover, not to
        -- freeze the rest of the entry, so their intent is carried across.
        UPDATE games SET cover_locked = 1
         WHERE metadata_locked = 1 AND cover_path IS NOT NULL;
        """,
    ),
    (
        4,
        "controller profiles, player order, and per-game overrides",
        """
        -- A saved pad layout. `guid` is the SDL GUID the mapping is keyed by,
        -- which is what both SDL and the community database match on, so a
        -- profile follows the physical pad rather than whichever port it
        -- happened to be plugged into.
        --
        -- `mapping` is the rendered SDL_GAMECONTROLLERCONFIG line. Storing the
        -- rendered form rather than the structure means a profile keeps
        -- working unchanged if the canonical model ever gains a button.
        CREATE TABLE controller_profiles (
            id          INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            guid        TEXT NOT NULL,
            device_name TEXT NOT NULL DEFAULT '',
            mapping     TEXT NOT NULL,
            -- 'database' | 'builtin' | 'user': where the layout came from, so
            -- the interface can distinguish a known-good mapping from a guess.
            source      TEXT NOT NULL DEFAULT 'user',
            -- Player 1 is 1, not 0: this is shown to people, not indexed into.
            player      INTEGER,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );

        -- One profile per pad model. Re-binding the same pad replaces its
        -- profile rather than accumulating duplicates that silently disagree.
        CREATE UNIQUE INDEX idx_controller_profiles_guid ON controller_profiles(guid);

        -- At most one pad per player slot. Without this, two pads could both
        -- claim player 1 and which one won would depend on row order.
        CREATE UNIQUE INDEX idx_controller_profiles_player
            ON controller_profiles(player) WHERE player IS NOT NULL;

        -- A game that wants a specific pad regardless of what else is plugged
        -- in: an arcade stick for arcade, a Pro Controller for Switch.
        CREATE TABLE game_controller_profiles (
            game_id     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            profile_id  INTEGER NOT NULL REFERENCES controller_profiles(id) ON DELETE CASCADE,
            player      INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (game_id, player)
        );

        CREATE INDEX idx_game_controller_profiles_game ON game_controller_profiles(game_id);
        """,
    ),
    (
        5,
        "indexes for the sorts and filters the interface actually offers",
        """
        -- Every one of these was measured on a 3000-game library before being
        -- added; none is here on the theory that an index is generally a good
        -- idea. Writes were unaffected within noise and the file grows about
        -- eight per cent.

        -- The biggest win by far, and the most common query in the whole
        -- application: picking a system in the sidebar, which Big Picture also
        -- runs once per system to build its shelves. The system index alone
        -- still needed a temporary B-tree to order the result; including
        -- sort_title means the rows come out of the index already in order.
        -- 8.3ms -> 2.3ms.
        CREATE INDEX idx_games_system_sort ON games(system, sort_title);

        -- "Recently played" and "Recently added" are two of the six sort
        -- options, and the first two shelves in Big Picture. Both were a full
        -- scan of the table followed by a sort. 11.4ms -> 6.7ms.
        --
        -- No DESC here on purpose: SQLite reads an index backwards perfectly
        -- well, so one index serves both directions.
        CREATE INDEX idx_games_last_played ON games(last_played);
        CREATE INDEX idx_games_added       ON games(added_at);
        CREATE INDEX idx_games_playtime    ON games(play_seconds);
        """,
    ),
    (
        6,
        "per-system emulator choice and arguments",
        """
        -- Which emulator runs a system, and what to pass it. Both were
        -- half-present before: `Launcher` accepted an `emulator_paths` mapping
        -- that nothing ever populated, so detection's pick was final and there
        -- was no way to say "use Xenia Canary for this system, not Edge", or
        -- "always start PCSX2 fullscreen".
        --
        -- Keyed by system id, which is what every lookup in the codebase uses.
        -- The old config file keyed some of these by emulator name and those
        -- entries could never be read back; see config.py.
        CREATE TABLE system_settings (
            system        TEXT PRIMARY KEY,
            -- Absolute path to an emulator binary, overriding detection.
            emulator_path TEXT,
            -- Appended to the command line for every game on this system.
            extra_args    TEXT,
            updated_at    TEXT NOT NULL
        );
        """,
    ),
    (
        7,
        "remember which games have no RetroAchievements set",
        """
        -- Matching a game to RetroAchievements is the expensive half: a hash
        -- where the algorithm is implemented, a rate-limited title search
        -- otherwise. Most libraries contain plenty of games RetroAchievements
        -- simply does not cover, and without a record of that, every launch
        -- would search for all of them again, for ever.
        --
        -- When this is set and `ra_game_id` is null, the answer is known and
        -- settled: this game has no achievement set. The user can still ask
        -- for a specific game to be tried again by hand, which clears it.
        ALTER TABLE games ADD COLUMN ra_checked_at TEXT;

        CREATE INDEX idx_games_ra_checked ON games(ra_checked_at);
        """,
    ),
]

SCHEMA_VERSION = max(version for version, _, _ in MIGRATIONS)

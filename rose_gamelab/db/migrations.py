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
]

SCHEMA_VERSION = max(version for version, _, _ in MIGRATIONS)

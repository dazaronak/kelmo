-- kelmo schema v1 (retrieval tables are additive; db.upgrade_retrieval brings older files up)

CREATE TABLE IF NOT EXISTS archive_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY,
    content         TEXT NOT NULL,
    kind            TEXT NOT NULL,
    domain          TEXT NOT NULL,
    project         TEXT,
    tags_json       TEXT NOT NULL DEFAULT '[]',
    source          TEXT,
    valid_from      TEXT,
    machine_tag     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    raw_note_path   TEXT,
    version         INTEGER NOT NULL DEFAULT 1
);

-- Replaced versions retain their own metadata, including nullable values.
CREATE TABLE IF NOT EXISTS memory_versions (
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    kind TEXT NOT NULL,
    domain TEXT NOT NULL,
    project TEXT,
    tags_json TEXT NOT NULL,
    valid_from TEXT,
    written_at TEXT NOT NULL,
    replaced_at TEXT NOT NULL,
    source TEXT,
    machine_tag TEXT,
    created_at TEXT,
    raw_note_path TEXT,
    PRIMARY KEY(memory_id, version)
);

-- Existing derived windows can be read without selecting a new chunking provider.
-- A chunk cut by a ChunkProfile is an exact slice content[start_char:end_char] of the record version it
-- names; imported legacy windows leave these columns NULL and are never ranked as current.
-- db.upgrade_retrieval adds the columns to an older table, since CREATE TABLE IF NOT EXISTS keeps it.
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    text TEXT NOT NULL,
    profile_id TEXT,
    memory_version INTEGER,
    content_sha256 TEXT,
    start_char INTEGER,
    end_char INTEGER,
    text_sha256 TEXT,
    UNIQUE(memory_id, idx)
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content='chunks',
    content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;

-- An earlier archive database keeps its own chunks_ad, which also clears its vec0 tables.
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', old.id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', old.id, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES(new.id, new.text);
END;

-- Retrieval identities. space_id/profile_id hash the canonical JSON of the whole EmbeddingSpace /
-- ChunkProfile, so a matching model name or dimension never makes two spaces one. verified = 0 marks
-- vectors kept as history whose space cannot be established; search never ranks them.
CREATE TABLE IF NOT EXISTS embedding_spaces (
    space_id    TEXT PRIMARY KEY,
    space_json  TEXT NOT NULL,
    verified    INTEGER NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunk_profiles (
    profile_id    TEXT PRIMARY KEY,
    profile_json  TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

-- Dense chunk vectors, float32 little-endian, one row per (space, chunk). The chunk's identity is
-- copied so a reused chunk id never inherits a vector cut from other text.
CREATE TABLE IF NOT EXISTS chunk_vectors (
    space_id        TEXT NOT NULL REFERENCES embedding_spaces(space_id),
    chunk_id        INTEGER NOT NULL,
    memory_id       TEXT NOT NULL,
    record_version  INTEGER,
    content_sha256  TEXT,
    profile_id      TEXT,
    idx             INTEGER NOT NULL,
    text_sha256     TEXT NOT NULL,
    embedding       BLOB NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (space_id, chunk_id)
);

-- Exact bytes of vectors whose embedding space was never recorded (the earlier archive's vec0 tables), copied before any
-- deletion can drop them. One row per (source table, source rowid, vector bytes): a second generation
-- under the same rowid is a second row, a repeat is ignored. memory_id/idx/text_sha256 are what the
-- source rowid pointed at when the copy was made, and stay NULL when that could not be established
-- (an orphan, or a rowid now taken by a chunk the current chunker cut). Never ranked, never verified.
CREATE TABLE IF NOT EXISTS unverified_vectors (
    id               INTEGER PRIMARY KEY,
    space_id         TEXT NOT NULL REFERENCES embedding_spaces(space_id),
    source_table     TEXT NOT NULL,
    source_rowid     INTEGER NOT NULL,
    embedding_sha256 TEXT NOT NULL,
    embedding        BLOB NOT NULL,
    -- Observed row mapping only; it does not prove which text produced the vector.
    memory_id        TEXT,
    idx              INTEGER,
    text_sha256      TEXT,
    preserved_at     TEXT NOT NULL,
    UNIQUE (source_table, source_rowid, embedding_sha256)
);

-- Vectors of a verified space are derived data and leave with their chunk; history stays.
CREATE TRIGGER IF NOT EXISTS chunks_ad_vectors AFTER DELETE ON chunks BEGIN
    DELETE FROM chunk_vectors WHERE chunk_id = old.id
      AND space_id IN (SELECT space_id FROM embedding_spaces WHERE verified = 1);
END;

-- Visibility of archived records: a forgotten memory keeps every byte it has
-- (row, FTS posting, vector) and is hidden from readers by this marker alone.
CREATE TABLE IF NOT EXISTS record_visibility (
    id           TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    forgotten_at TEXT NOT NULL,
    reason       TEXT
);

-- Opt-in search telemetry (memory/search_log.py), written outside every caller transaction and read
-- only through the administrator capability. The first twelve columns are the earlier archive's; search_log.ensure adds
-- the identity columns to a table an older archive created. Redacted rows hold hashes, never query text.
CREATE TABLE IF NOT EXISTS search_log (
    id              INTEGER PRIMARY KEY,
    at              TEXT NOT NULL,
    machine_tag     TEXT,
    client          TEXT,
    query_en        TEXT NOT NULL,
    query_ru        TEXT NOT NULL,
    mode            TEXT NOT NULL,
    "limit"         INTEGER NOT NULL,
    filters_json    TEXT NOT NULL DEFAULT '{}',
    semantic        INTEGER NOT NULL,
    results_json    TEXT NOT NULL DEFAULT '[]',
    ms              INTEGER,
    privacy         TEXT,
    variants_json   TEXT,
    retrieval_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_search_log_at ON search_log(at);

CREATE INDEX IF NOT EXISTS idx_memories_kind    ON memories(kind);
CREATE INDEX IF NOT EXISTS idx_memories_domain  ON memories(domain);
CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);
CREATE INDEX IF NOT EXISTS idx_memories_machine ON memories(machine_tag);

-- Vector index (sqlite-vec, 384 dim fastembed)
CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
    embedding float[384]
);

-- BM25 full-text index (unicode61 tokenizer)
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    content='memories',
    content_rowid='rowid',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.rowid, old.content);
    DELETE FROM memories_vec WHERE rowid = old.rowid;
END;

-- UPDATE trigger: delete-sentinel pattern per FTS5 external-content docs
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.rowid, old.content);
    INSERT INTO memories_fts(rowid, content) VALUES(new.rowid, new.content);
END;

-- Graph: entities
CREATE TABLE IF NOT EXISTS entities (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    type            TEXT NOT NULL,
    file_path       TEXT,
    line_number     INTEGER,
    in_degree       INTEGER DEFAULT 0,
    out_degree      INTEGER DEFAULT 0,
    community_id    INTEGER,
    surprise_score  REAL,
    is_god_node     INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);

-- Graph: edges
CREATE TABLE IF NOT EXISTS edges (
    source_id   TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    relation    TEXT NOT NULL,
    confidence  TEXT NOT NULL DEFAULT 'EXTRACTED',
    weight      REAL DEFAULT 1.0,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (source_id, target_id, relation),
    FOREIGN KEY (source_id) REFERENCES entities(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES entities(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);

-- FTS5 trigram for entity name search
CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(
    name, type,
    content='entities',
    content_rowid='rowid',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS entities_ai AFTER INSERT ON entities BEGIN
    INSERT INTO entities_fts(rowid, name, type) VALUES (new.rowid, new.name, new.type);
END;

CREATE TRIGGER IF NOT EXISTS entities_ad AFTER DELETE ON entities BEGIN
    INSERT INTO entities_fts(entities_fts, rowid, name, type) VALUES('delete', old.rowid, old.name, old.type);
END;

CREATE TRIGGER IF NOT EXISTS entities_au AFTER UPDATE ON entities BEGIN
    INSERT INTO entities_fts(entities_fts, rowid, name, type) VALUES('delete', old.rowid, old.name, old.type);
    INSERT INTO entities_fts(rowid, name, type) VALUES(new.rowid, new.name, new.type);
END;

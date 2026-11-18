BEGIN IMMEDIATE;
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'admin')),
    created_at TEXT NOT NULL
);
CREATE TABLE photos (
    id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    uploaded_at TEXT NOT NULL,
    format TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size > 0),
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    original_retained INTEGER NOT NULL CHECK (original_retained IN (0, 1))
);
CREATE INDEX photos_owner_uploaded ON photos(owner_id, uploaded_at DESC);
CREATE TABLE photo_metadata (
    photo_id TEXT PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('extracted', 'missing', 'unsupported', 'failed')),
    uploaded_at TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    fields_json TEXT NOT NULL CHECK (json_valid(fields_json)),
    warnings_json TEXT NOT NULL CHECK (json_valid(warnings_json)),
    error_code TEXT
);
CREATE TABLE login_attempts (
    key TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL,
    window_start INTEGER NOT NULL
);
CREATE TABLE schema_migrations (version TEXT PRIMARY KEY);
INSERT INTO schema_migrations(version) VALUES ('001');
COMMIT;

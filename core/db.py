import json, os, shutil, sqlite3, time
from datetime import datetime

from core import config

LEGACY_DB = "logibot.db"
LEGACY_BACKUPS = "backups"


def _has_data(path):
    try:
        probe = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return any(probe.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("presets", "events", "signups", "profiles"))
        finally:
            probe.close()
    except sqlite3.Error:
        return False


def _restore_source():
    if os.path.abspath(config.DB_PATH) != os.path.abspath(LEGACY_DB) and os.path.exists(LEGACY_DB) and _has_data(LEGACY_DB):
        return LEGACY_DB
    for folder in dict.fromkeys([config.BACKUP_DIR, LEGACY_BACKUPS]):
        if os.path.isdir(folder):
            for name in sorted((file for file in os.listdir(folder) if file.startswith("logibot-") and file.endswith(".db")), reverse=True):
                if _has_data(os.path.join(folder, name)):
                    return os.path.join(folder, name)
    return None


_folder = os.path.dirname(config.DB_PATH)
if _folder:
    os.makedirs(_folder, exist_ok=True)
if not os.path.exists(config.DB_PATH):
    _source = _restore_source()
    if _source:
        shutil.copy2(_source, config.DB_PATH)
        RESTORED_FROM = _source

_conn = sqlite3.connect(config.DB_PATH)
_conn.row_factory = sqlite3.Row

_conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER, title TEXT, fb_link TEXT, location TEXT,
        start_ts INTEGER, close_ts INTEGER,
        thread_id INTEGER, cars_message_id INTEGER,
        preclose_pinged INTEGER DEFAULT 0,
        closed INTEGER DEFAULT 0,
        reminded INTEGER DEFAULT 0,
        cancelled INTEGER DEFAULT 0,
        custom_cutoff INTEGER DEFAULT 0,
        summarised INTEGER DEFAULT 0,
        kind TEXT DEFAULT 'event',
        markers TEXT,
        preset_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS presets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER, name TEXT, kind TEXT, location TEXT,
        weekday INTEGER, time TEXT, markers TEXT, content TEXT,
        forum_id INTEGER, draft_thread_id INTEGER, created_by INTEGER,
        status TEXT DEFAULT 'draft',
        auto_enabled INTEGER DEFAULT 0,
        edit_forum_id INTEGER, editor_id INTEGER,
        repeat TEXT DEFAULT 'week', repeat_every INTEGER DEFAULT 1, anchor TEXT,
        draft_started INTEGER
    );
    CREATE TABLE IF NOT EXISTS profiles (
        guild_id INTEGER, user_id INTEGER,
        capacity INTEGER, avoid TEXT, organised TEXT, address TEXT, kit TEXT,
        area TEXT, need TEXT, updated_at INTEGER,
        PRIMARY KEY (guild_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id INTEGER PRIMARY KEY,
        manager_roles TEXT, category_name TEXT, forum_name TEXT, social_forum_name TEXT,
        area_matching INTEGER DEFAULT 0, log_channel_id INTEGER,
        category_id INTEGER, event_forum_id INTEGER, social_forum_id INTEGER,
        log_guide_id INTEGER, log_guide_hash TEXT, action_log_channel_id INTEGER,
        public_addresses INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS signups (
        event_id INTEGER, user_id INTEGER, role TEXT,
        capacity INTEGER, avoid TEXT, organised TEXT, address TEXT, kit TEXT,
        area TEXT, need TEXT DEFAULT 'both', details TEXT,
        PRIMARY KEY (event_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER, user_id INTEGER, label TEXT, kit TEXT,
        driver_id INTEGER, return_id INTEGER, need TEXT DEFAULT 'both',
        area TEXT, locked INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS preset_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        preset_id INTEGER, filename TEXT, spoiler INTEGER DEFAULT 0, data BLOB
    );
    CREATE TABLE IF NOT EXISTS sessions (
        user_id INTEGER PRIMARY KEY,
        guild_id INTEGER, kind TEXT, event_id INTEGER, role TEXT
    );
    CREATE TABLE IF NOT EXISTS actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER, ts INTEGER, text TEXT
    );
    """
)


def _ensure(table, column, decl):
    if column not in {row[1] for row in _conn.execute(f"PRAGMA table_info({table})")}:
        _conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


for tableName, columnName, definition in [
    ("events", "summarised", "INTEGER DEFAULT 0"), ("guild_settings", "public_addresses", "INTEGER DEFAULT 1"),
    ("guild_settings", "action_log_channel_id", "INTEGER"), ("guild_settings", "log_guide_id", "INTEGER"), ("guild_settings", "log_guide_hash", "TEXT"),
    ("presets", "draft_started", "INTEGER"), ("presets", "repeat", "TEXT DEFAULT 'week'"), ("presets", "repeat_every", "INTEGER DEFAULT 1"), ("presets", "anchor", "TEXT"),
    ("events", "preset_id", "INTEGER"), ("presets", "edit_forum_id", "INTEGER"), ("presets", "editor_id", "INTEGER"),
    ("guild_settings", "category_id", "INTEGER"), ("guild_settings", "event_forum_id", "INTEGER"),
    ("guild_settings", "social_forum_id", "INTEGER"),
    ("events", "kind", "TEXT DEFAULT 'event'"), ("events", "markers", "TEXT"),
    ("guild_settings", "social_forum_name", "TEXT"), ("signups", "details", "TEXT"),
    ("events", "cancelled", "INTEGER DEFAULT 0"), ("events", "custom_cutoff", "INTEGER DEFAULT 0"),
    ("profiles", "area", "TEXT"), ("profiles", "need", "TEXT"), ("profiles", "updated_at", "INTEGER"),
    ("guild_settings", "area_matching", "INTEGER DEFAULT 0"), ("guild_settings", "log_channel_id", "INTEGER"),
    ("signups", "area", "TEXT"), ("signups", "need", "TEXT DEFAULT 'both'"),
    ("assignments", "return_id", "INTEGER"), ("assignments", "need", "TEXT DEFAULT 'both'"),
    ("assignments", "area", "TEXT"),
]:
    _ensure(tableName, columnName, definition)
_conn.commit()


def q(statement, args=()):
    return _conn.execute(statement, args).fetchall()


def q1(statement, args=()):
    return _conn.execute(statement, args).fetchone()


def run(statement, args=()):
    cursor = _conn.execute(statement, args)
    _conn.commit()
    return cursor.lastrowid


def claim(statement, args=()):
    cursor = _conn.execute(statement, args)
    _conn.commit()
    return cursor.rowcount > 0


ACTION_LIMIT = 100


def remember(guild_id, text):
    _conn.execute("INSERT INTO actions (guild_id, ts, text) VALUES (?,?,?)", (guild_id, int(time.time()), text))
    _conn.execute("DELETE FROM actions WHERE id NOT IN (SELECT id FROM actions ORDER BY id DESC LIMIT ?)", (ACTION_LIMIT,))
    _conn.commit()


def recent_actions(guild_id=None, limit=ACTION_LIMIT):
    if guild_id is None:
        return q("SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,))
    return q("SELECT * FROM actions WHERE guild_id=? ORDER BY id DESC LIMIT ?", (guild_id, limit))


def dumps(value):
    return json.dumps(value)


def loads(value):
    return json.loads(value) if value else []


def purge(now: int | None = None) -> tuple[int, int]:
    now = now or int(time.time())
    cutoff = now - config.EVENT_RETENTION_DAYS * 86400
    old = [row["id"] for row in q("SELECT id FROM events WHERE start_ts < ?", (cutoff,))]
    for eventId in old:
        _conn.execute("DELETE FROM signups WHERE event_id=?", (eventId,))
        _conn.execute("DELETE FROM assignments WHERE event_id=?", (eventId,))
        _conn.execute("DELETE FROM events WHERE id=?", (eventId,))
    pcut = now - config.PROFILE_RETENTION_DAYS * 86400
    cursor = _conn.execute("DELETE FROM profiles WHERE COALESCE(updated_at, 0) < ?", (pcut,))
    _conn.commit()
    return len(old), cursor.rowcount


def backup() -> str:
    os.makedirs(config.BACKUP_DIR, exist_ok=True)
    path = os.path.join(config.BACKUP_DIR, f"logibot-{datetime.now():%Y%m%d-%H%M}.db")
    dest = sqlite3.connect(path)
    with dest:
        _conn.backup(dest)
    dest.close()
    files = sorted(file for file in os.listdir(config.BACKUP_DIR) if file.startswith("logibot-"))
    for file in files[: -config.BACKUP_KEEP]:
        os.remove(os.path.join(config.BACKUP_DIR, file))
    return path

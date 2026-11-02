import os
import sqlite3
import time

conn = sqlite3.connect(os.getenv("LOCKOUT_DB", "lockout.db"))
conn.row_factory = sqlite3.Row
conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS users (
        discord_id INTEGER PRIMARY KEY,
        handle TEXT UNIQUE NOT NULL,
        real_name TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS rooms (channel_id INTEGER PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS extra_rooms (channel_id INTEGER PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS active (channel_id INTEGER PRIMARY KEY, data TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id INTEGER, p1 TEXT, p2 TEXT,
        score1 INTEGER, score2 INTEGER,
        winner TEXT, tiebreak INTEGER, finished_at INTEGER
    );
    """
)


def user(discord_id: int):
    return conn.execute("SELECT * FROM users WHERE discord_id=?", (discord_id,)).fetchone()


def by_handle(handle: str):
    return conn.execute("SELECT * FROM users WHERE handle=? COLLATE NOCASE", (handle,)).fetchone()


def save_user(discord_id: int, handle: str, real_name: str):
    conn.execute(
        "INSERT OR REPLACE INTO users VALUES (?,?,?)", (discord_id, handle, real_name)
    )
    conn.commit()


def delete_user(discord_id: int):
    conn.execute("DELETE FROM users WHERE discord_id=?", (discord_id,))
    conn.commit()


ROOM_TABLES = ("rooms", "extra_rooms")  # competition rooms, practice rooms


def all_rooms(table="rooms") -> list[int]:
    assert table in ROOM_TABLES
    return [r["channel_id"] for r in conn.execute(f"SELECT channel_id FROM {table} ORDER BY rowid")]


def add_rooms(channel_ids: list[int], table="rooms"):
    assert table in ROOM_TABLES
    conn.executemany(f"INSERT INTO {table} VALUES (?)", [(c,) for c in channel_ids])
    conn.commit()


def remove_rooms(channel_ids: list[int], table="rooms"):
    assert table in ROOM_TABLES
    conn.executemany(f"DELETE FROM {table} WHERE channel_id=?", [(c,) for c in channel_ids])
    conn.commit()


def is_room(channel_id: int, table="rooms") -> bool:
    assert table in ROOM_TABLES
    return conn.execute(f"SELECT 1 FROM {table} WHERE channel_id=?", (channel_id,)).fetchone() is not None


def all_users():
    return conn.execute("SELECT * FROM users ORDER BY real_name COLLATE NOCASE").fetchall()


def all_results():
    return conn.execute("SELECT * FROM results ORDER BY id").fetchall()


def save_result(channel_id, p1, p2, s1, s2, winner, tiebreak):
    conn.execute(
        "INSERT INTO results (channel_id,p1,p2,score1,score2,winner,tiebreak,finished_at) VALUES (?,?,?,?,?,?,?,?)",
        (channel_id, p1, p2, s1, s2, winner, int(tiebreak), int(time.time())),
    )
    conn.commit()


def get_config(key: str):
    row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_config(key: str, value):
    conn.execute("INSERT OR REPLACE INTO config VALUES (?,?)", (key, str(value)))
    conn.commit()


def save_active(channel_id: int, data: str):
    conn.execute("INSERT OR REPLACE INTO active VALUES (?,?)", (channel_id, data))
    conn.commit()


def drop_active(channel_id: int):
    conn.execute("DELETE FROM active WHERE channel_id=?", (channel_id,))
    conn.commit()


def all_active() -> list[tuple[int, str]]:
    return [(r["channel_id"], r["data"]) for r in conn.execute("SELECT channel_id, data FROM active")]

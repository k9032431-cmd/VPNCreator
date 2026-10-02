import os
import time
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT,
    full_name   TEXT,
    role        TEXT NOT NULL DEFAULT 'new',   -- new | pending | user | admin | banned
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS servers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id          INTEGER NOT NULL,
    name              TEXT NOT NULL,
    host              TEXT NOT NULL,
    port              INTEGER NOT NULL DEFAULT 22,
    username          TEXT NOT NULL,
    auth_type         TEXT NOT NULL,           -- password | key
    secret            TEXT NOT NULL,           -- зашифрованный пароль / приватный ключ
    installed_version INTEGER NOT NULL DEFAULT 0,
    created_at        INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id    INTEGER NOT NULL,
    server_id   INTEGER NOT NULL,
    name        TEXT NOT NULL,
    value       TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL UNIQUE,
    content  BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""

Row = dict[str, Any]


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def _one(self, sql: str, args: tuple = ()) -> Row | None:
        async with self.conn.execute(sql, args) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def _all(self, sql: str, args: tuple = ()) -> list[Row]:
        async with self.conn.execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def _exec(self, sql: str, args: tuple = ()) -> int:
        cur = await self.conn.execute(sql, args)
        await self.conn.commit()
        return cur.lastrowid

    async def _scalar(self, sql: str, args: tuple = ()) -> int:
        async with self.conn.execute(sql, args) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    # ---------- users ----------
    async def sync_user(self, uid: int, username: str | None, full_name: str, is_env_admin: bool) -> Row:
        user = await self.get_user(uid)
        if user is None:
            role = "admin" if is_env_admin else "new"
            await self._exec(
                "INSERT INTO users (id, username, full_name, role, created_at) VALUES (?,?,?,?,?)",
                (uid, username, full_name, role, int(time.time())),
            )
        else:
            role = "admin" if is_env_admin else user["role"]
            if (user["username"], user["full_name"], user["role"]) != (username, full_name, role):
                await self._exec(
                    "UPDATE users SET username=?, full_name=?, role=? WHERE id=?",
                    (username, full_name, role, uid),
                )
        return await self.get_user(uid)

    async def get_user(self, uid: int) -> Row | None:
        return await self._one("SELECT * FROM users WHERE id=?", (uid,))

    async def ensure_user(self, uid: int) -> None:
        await self._exec(
            "INSERT OR IGNORE INTO users (id, role, created_at) VALUES (?, 'new', ?)",
            (uid, int(time.time())),
        )

    async def set_role(self, uid: int, role: str) -> None:
        await self._exec("UPDATE users SET role=? WHERE id=?", (role, uid))

    async def list_users(self, roles: tuple[str, ...], offset: int, limit: int) -> list[Row]:
        q = ",".join("?" * len(roles))
        return await self._all(
            f"SELECT * FROM users WHERE role IN ({q}) ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*roles, limit, offset),
        )

    async def count_users(self, roles: tuple[str, ...]) -> int:
        q = ",".join("?" * len(roles))
        return await self._scalar(f"SELECT COUNT(*) FROM users WHERE role IN ({q})", roles)

    async def admin_ids(self) -> list[int]:
        return [r["id"] for r in await self._all("SELECT id FROM users WHERE role='admin'")]

    # ---------- servers ----------
    async def add_server(self, owner_id: int, name: str, host: str, port: int, username: str,
                         auth_type: str, secret: str) -> int:
        return await self._exec(
            "INSERT INTO servers (owner_id, name, host, port, username, auth_type, secret, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (owner_id, name, host, port, username, auth_type, secret, int(time.time())),
        )

    async def get_server(self, sid: int, owner_id: int | None = None) -> Row | None:
        if owner_id is None:
            return await self._one("SELECT * FROM servers WHERE id=?", (sid,))
        return await self._one("SELECT * FROM servers WHERE id=? AND owner_id=?", (sid, owner_id))

    async def list_servers(self, owner_id: int) -> list[Row]:
        return await self._all("SELECT * FROM servers WHERE owner_id=? ORDER BY id", (owner_id,))

    async def count_servers(self, owner_id: int | None = None) -> int:
        if owner_id is None:
            return await self._scalar("SELECT COUNT(*) FROM servers")
        return await self._scalar("SELECT COUNT(*) FROM servers WHERE owner_id=?", (owner_id,))

    async def delete_server(self, sid: int) -> None:
        await self._exec("DELETE FROM keys WHERE server_id=?", (sid,))
        await self._exec("DELETE FROM servers WHERE id=?", (sid,))

    async def set_server_version(self, sid: int, version: int) -> None:
        await self._exec("UPDATE servers SET installed_version=? WHERE id=?", (version, sid))

    # ---------- keys ----------
    async def add_key(self, owner_id: int, server_id: int, name: str, value: str) -> int:
        return await self._exec(
            "INSERT INTO keys (owner_id, server_id, name, value, created_at) VALUES (?,?,?,?,?)",
            (owner_id, server_id, name, value, int(time.time())),
        )

    async def get_key(self, kid: int, owner_id: int) -> Row | None:
        return await self._one(
            "SELECT k.*, s.name AS server_name FROM keys k LEFT JOIN servers s ON s.id=k.server_id "
            "WHERE k.id=? AND k.owner_id=?",
            (kid, owner_id),
        )

    async def list_keys(self, owner_id: int, offset: int, limit: int) -> list[Row]:
        return await self._all(
            "SELECT k.*, s.name AS server_name FROM keys k LEFT JOIN servers s ON s.id=k.server_id "
            "WHERE k.owner_id=? ORDER BY k.id DESC LIMIT ? OFFSET ?",
            (owner_id, limit, offset),
        )

    async def count_keys(self, owner_id: int | None = None) -> int:
        if owner_id is None:
            return await self._scalar("SELECT COUNT(*) FROM keys")
        return await self._scalar("SELECT COUNT(*) FROM keys WHERE owner_id=?", (owner_id,))

    async def key_name_exists(self, server_id: int, name: str) -> bool:
        return bool(await self._scalar(
            "SELECT COUNT(*) FROM keys WHERE server_id=? AND name=?", (server_id, name)))

    async def delete_key(self, kid: int) -> None:
        await self._exec("DELETE FROM keys WHERE id=?", (kid,))

    # ---------- files (скрипты) ----------
    async def list_files(self) -> list[Row]:
        return await self._all("SELECT id, name, length(content) AS size FROM files ORDER BY id")

    async def all_files(self) -> list[Row]:
        return await self._all("SELECT name, content FROM files ORDER BY id")

    async def put_file(self, name: str, content: bytes) -> None:
        await self._exec(
            "INSERT INTO files (name, content) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET content=excluded.content",
            (name, content),
        )

    async def get_file(self, fid: int) -> Row | None:
        return await self._one("SELECT id, name FROM files WHERE id=?", (fid,))

    async def delete_file(self, fid: int) -> None:
        await self._exec("DELETE FROM files WHERE id=?", (fid,))

    # ---------- settings ----------
    async def get(self, k: str, default: str = "") -> str:
        row = await self._one("SELECT v FROM settings WHERE k=?", (k,))
        return row["v"] if row and row["v"] is not None else default

    async def set(self, k: str, v: str) -> None:
        await self._exec(
            "INSERT INTO settings (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    async def config_version(self) -> int:
        return int(await self.get("config_version", "1"))

    async def bump_version(self) -> int:
        v = await self.config_version() + 1
        await self.set("config_version", str(v))
        return v

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Any

from tglol.config import Config


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT,
    telegram_user_id INTEGER,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    session_path TEXT NOT NULL,
    json_original_path TEXT,
    json_effective_path TEXT,
    json_source TEXT NOT NULL,
    twofa_password TEXT,
    source_type TEXT NOT NULL,
    worker_id INTEGER,
    department_id INTEGER,
    account_stage TEXT NOT NULL DEFAULT 'nereg',
    status TEXT NOT NULL,
    created_by INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER,
    name TEXT NOT NULL,
    department_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proxies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proxy TEXT NOT NULL,
    worker_id INTEGER,
    status TEXT NOT NULL DEFAULT 'stored',
    created_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Account:
    id: int
    phone: str | None
    telegram_user_id: int | None
    username: str | None
    first_name: str | None
    last_name: str | None
    session_path: str
    json_original_path: str | None
    json_effective_path: str | None
    json_source: str
    twofa_password: str | None
    source_type: str
    worker_id: int | None
    department_id: int | None
    account_stage: str
    status: str
    created_by: int | None
    created_at: str
    updated_at: str


def connect(config: Config) -> sqlite3.Connection:
    connection = sqlite3.connect(config.db_path)
    connection.row_factory = sqlite3.Row
    return connection


def init_db(config: Config) -> None:
    with connect(config) as connection:
        connection.executescript(SCHEMA)
        _ensure_column(connection, "accounts", "worker_id", "INTEGER")
        _ensure_column(connection, "accounts", "department_id", "INTEGER")
        _ensure_column(connection, "accounts", "account_stage", "TEXT NOT NULL DEFAULT 'nereg'")
        connection.execute("UPDATE workers SET department_id = NULL")
        connection.execute("UPDATE accounts SET department_id = NULL")


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def add_account(config: Config, values: dict[str, Any]) -> int:
    columns = ", ".join(values.keys())
    placeholders = ", ".join(f":{key}" for key in values)
    with connect(config) as connection:
        cursor = connection.execute(
            f"INSERT INTO accounts ({columns}) VALUES ({placeholders})",
            values,
        )
        return int(cursor.lastrowid)


def list_accounts(
    config: Config,
    limit: int = 20,
    offset: int = 0,
    worker_id: int | None | str = "any",
    account_stage: str | None = None,
    department_id: int | None | str = "any",
) -> list[Account]:
    clauses: list[str] = []
    params: list[Any] = []
    if worker_id is None:
        clauses.append("worker_id IS NULL")
    elif worker_id != "any":
        clauses.append("worker_id = ?")
        params.append(worker_id)
    if account_stage is not None:
        clauses.append("account_stage = ?")
        params.append(account_stage)
    if department_id is None:
        clauses.append("department_id IS NULL")
    elif department_id != "any":
        clauses.append("department_id = ?")
        params.append(department_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with connect(config) as connection:
        rows = connection.execute(
            f"SELECT * FROM accounts {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return [Account(**dict(row)) for row in rows]


def get_account(config: Config, account_id: int) -> Account | None:
    with connect(config) as connection:
        row = connection.execute(
            "SELECT * FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
    return Account(**dict(row)) if row else None


def count_accounts(config: Config, worker_id: int | None | str = "any") -> int:
    return count_accounts_by_stage(config, worker_id=worker_id)


def count_accounts_by_stage(
    config: Config,
    worker_id: int | None | str = "any",
    account_stage: str | None = None,
    department_id: int | None | str = "any",
) -> int:
    clauses: list[str] = []
    params: list[Any] = []
    if worker_id is None:
        clauses.append("worker_id IS NULL")
    elif worker_id != "any":
        clauses.append("worker_id = ?")
        params.append(worker_id)
    if account_stage is not None:
        clauses.append("account_stage = ?")
        params.append(account_stage)
    if department_id is None:
        clauses.append("department_id IS NULL")
    elif department_id != "any":
        clauses.append("department_id = ?")
        params.append(department_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with connect(config) as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM accounts {where}", params).fetchone()[0])


def set_account_stage(config: Config, account_id: int, account_stage: str) -> None:
    if account_stage not in {"nereg", "reg"}:
        raise ValueError("unknown account stage")
    with connect(config) as connection:
        connection.execute(
            "UPDATE accounts SET account_stage = ?, updated_at = datetime('now') WHERE id = ?",
            (account_stage, account_id),
        )


def add_worker(
    config: Config,
    *,
    name: str,
    telegram_id: int | None,
    department_id: int | None,
    created_at: str,
) -> int:
    with connect(config) as connection:
        cursor = connection.execute(
            "INSERT INTO workers (telegram_id, name, department_id, created_at) VALUES (?, ?, ?, ?)",
            (telegram_id, name, department_id, created_at),
        )
        return int(cursor.lastrowid)


def list_workers(config: Config) -> list[sqlite3.Row]:
    with connect(config) as connection:
        return connection.execute("SELECT * FROM workers ORDER BY id DESC").fetchall()


def get_worker(config: Config, worker_id: int) -> sqlite3.Row | None:
    with connect(config) as connection:
        return connection.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()


def get_worker_by_telegram_id(config: Config, telegram_id: int) -> sqlite3.Row | None:
    with connect(config) as connection:
        return connection.execute("SELECT * FROM workers WHERE telegram_id = ?", (telegram_id,)).fetchone()


def worker_exists(config: Config, worker_id: int) -> bool:
    with connect(config) as connection:
        row = connection.execute("SELECT 1 FROM workers WHERE id = ?", (worker_id,)).fetchone()
    return row is not None


def delete_worker(config: Config, worker_id: int) -> None:
    with connect(config) as connection:
        connection.execute("UPDATE accounts SET worker_id = NULL, department_id = NULL WHERE worker_id = ?", (worker_id,))
        connection.execute("UPDATE proxies SET worker_id = NULL WHERE worker_id = ?", (worker_id,))
        connection.execute("DELETE FROM workers WHERE id = ?", (worker_id,))


def assign_account_to_worker(config: Config, account_id: int, worker_id: int | None) -> None:
    with connect(config) as connection:
        connection.execute(
            "UPDATE accounts SET worker_id = ?, department_id = ?, updated_at = datetime('now') WHERE id = ?",
            (worker_id, None, account_id),
        )


def add_proxy(config: Config, proxy: str, created_at: str) -> int:
    with connect(config) as connection:
        cursor = connection.execute(
            "INSERT INTO proxies (proxy, created_at) VALUES (?, ?)",
            (proxy, created_at),
        )
        return int(cursor.lastrowid)


def list_proxies(config: Config) -> list[sqlite3.Row]:
    with connect(config) as connection:
        return connection.execute("SELECT * FROM proxies ORDER BY id DESC LIMIT 50").fetchall()


def proxy_exists(config: Config, proxy_id: int) -> bool:
    with connect(config) as connection:
        row = connection.execute("SELECT 1 FROM proxies WHERE id = ?", (proxy_id,)).fetchone()
    return row is not None


def assign_proxy_to_worker(config: Config, proxy_id: int, worker_id: int | None) -> None:
    with connect(config) as connection:
        connection.execute(
            "UPDATE proxies SET worker_id = ? WHERE id = ?",
            (worker_id, proxy_id),
        )

"""Thread-safe MySQL connection pool based on pymysql + DBUtils."""

from __future__ import annotations

from typing import Any

import pymysql
from dbutils.pooled_db import PooledDB

from config import Config

_pool: PooledDB | None = None


def get_pool() -> PooledDB:
    global _pool
    if _pool is None:
        _pool = PooledDB(
            creator=pymysql,
            maxconnections=Config.DB_POOL_SIZE,
            mincached=2,
            maxcached=Config.DB_POOL_SIZE,
            blocking=True,
            host=Config.DB_HOST,
            port=Config.DB_PORT,
            user=Config.DB_USER,
            password=Config.DB_PASSWORD,
            database=Config.DB_NAME,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )
    return _pool


class Database:
    """Thin wrapper around the connection pool for convenient queries."""

    def __init__(self) -> None:
        self._pool = get_pool()

    def execute(
        self, sql: str, args: tuple | None = None
    ) -> list[dict[str, Any]]:
        conn = self._pool.connection()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                if cur.description:
                    return cur.fetchall()
                return []
        finally:
            conn.close()

    def execute_many(self, sql: str, args_list: list[tuple]) -> int:
        conn = self._pool.connection()
        try:
            with conn.cursor() as cur:
                return cur.executemany(sql, args_list)
        finally:
            conn.close()

    def insert_id(self, sql: str, args: tuple | None = None) -> int:
        conn = self._pool.connection()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return cur.lastrowid
        finally:
            conn.close()

"""Short, nest-safe ownership of a SQLite logical read snapshot."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def read_snapshot(connection: sqlite3.Connection) -> Iterator[None]:
    """Pin one logical read without ending a caller-owned transaction."""

    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN")
    try:
        yield
    except BaseException as operation_error:
        if owns_transaction:
            try:
                connection.rollback()
            except sqlite3.Error as cleanup_error:
                raise operation_error from cleanup_error
        raise
    else:
        if owns_transaction:
            connection.rollback()

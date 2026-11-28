"""Registry of supported database engines.

To add an engine (e.g. PostgreSQL): subclass Driver in a new module and add it here.
"""
from .base import Driver
from .mssql import MSSQLDriver
from .mysql import MySQLDriver
from .postgres import PostgresDriver
from .sqlite import SQLiteDriver

_DRIVERS: dict[str, Driver] = {d.key: d for d in (SQLiteDriver(), MySQLDriver(), PostgresDriver(), MSSQLDriver())}


def get_driver(key: str) -> Driver:
    try:
        return _DRIVERS[key]
    except KeyError:
        raise ValueError(f"Unknown database type: {key}") from None


def all_drivers() -> list[Driver]:
    return list(_DRIVERS.values())


__all__ = ["Driver", "get_driver", "all_drivers"]

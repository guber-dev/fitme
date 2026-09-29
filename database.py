"""SQLite locally, TLS PostgreSQL for hosts with ephemeral disks."""
from contextlib import contextmanager
import sqlite3


class Row(dict):
    def __getitem__(self, key):
        return tuple(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


def row_factory(cursor):
    columns = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Row(zip(columns, values))


class Postgres:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=()):
        return self.connection.execute(sql.replace('?', '%s'), params)

    def executescript(self, sql):
        for statement in sql.split(';'):
            if statement.strip():
                self.execute(statement.replace(' BLOB', ' BYTEA').replace(' REAL', ' DOUBLE PRECISION'))


@contextmanager
def connect(path, database_url=None):
    if database_url:
        import psycopg
        # Serialize transactions across deploys as well as threads. Quota checks
        # and reservations must commit together before a paid request can run.
        with psycopg.connect(database_url, row_factory=row_factory, connect_timeout=15,
                             sslmode='require', options='-c statement_timeout=20000') as connection:
            connection.execute('SELECT pg_advisory_xact_lock(741926482)')
            yield Postgres(connection)
    else:
        db = sqlite3.connect(path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA secure_delete=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

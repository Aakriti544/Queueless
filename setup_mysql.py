"""Idempotently configure the local QueueLess MySQL database and account.

Credentials are read from the same project-root ``.env`` file and with the
same override behavior as Django settings. No credential values are logged.
"""

import os
import re
import sys
from pathlib import Path

import MySQLdb
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)


def required_env(name, *, preserve=False):
    value = os.environ.get(name, "")
    if not value.strip():
        raise ValueError(f"{name} must be configured in the project .env file")
    return value if preserve else value.strip()


def quote_identifier(value, name):
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ValueError(f"{name} may contain only letters, numbers, and underscores")
    return f"`{value}`"


def main():
    database = required_env("DB_NAME")
    app_user = required_env("DB_USER")
    # Preserve the exact secret bytes dotenv provides; Django passes this
    # value directly to mysqlclient as well.
    app_password = required_env("DB_PASSWORD", preserve=True)
    db_host = required_env("DB_HOST")
    db_port = int(required_env("DB_PORT"))
    root_password = required_env("MYSQL_ROOT_PASSWORD", preserve=True)
    root_host = os.environ.get("MYSQL_ROOT_HOST", db_host).strip() or db_host
    root_port = int(os.environ.get("MYSQL_ROOT_PORT", str(db_port)))

    quote_identifier(database, "DB_NAME")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", app_user):
        raise ValueError("DB_USER may contain only letters, numbers, periods, underscores, and hyphens")
    if not (1 <= db_port <= 65535 and 1 <= root_port <= 65535):
        raise ValueError("Database ports must be between 1 and 65535")

    print("=== QueueLess MySQL Setup ===")
    connection = None
    try:
        connection = MySQLdb.connect(
            host=root_host,
            port=root_port,
            user="root",
            passwd=root_password,
            connect_timeout=10,
            charset="utf8mb4",
            autocommit=True,
        )
        with connection.cursor() as cursor:
            db_identifier = quote_identifier(database, "DB_NAME")
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS {db_identifier} "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )

            # Keep both local account variants because Windows may identify a
            # TCP loopback client as either 127.0.0.1 or localhost.
            for account_host in ("localhost", "127.0.0.1"):
                cursor.execute(
                    "CREATE USER IF NOT EXISTS %s@%s IDENTIFIED BY %s",
                    (app_user, account_host, app_password),
                )
                cursor.execute(
                    "ALTER USER %s@%s IDENTIFIED BY %s",
                    (app_user, account_host, app_password),
                )
                cursor.execute(
                    f"GRANT ALL PRIVILEGES ON {db_identifier}.* TO %s@%s",
                    (app_user, account_host),
                )

        # Verify the exact TCP endpoint and credentials Django will use.
        app_connection = MySQLdb.connect(
            host=db_host,
            port=db_port,
            user=app_user,
            passwd=app_password,
            db=database,
            connect_timeout=10,
            charset="utf8mb4",
        )
        try:
            with app_connection.cursor() as cursor:
                cursor.execute("SELECT DATABASE(), CURRENT_USER()")
                actual_database, _actual_account = cursor.fetchone()
                if actual_database != database:
                    raise RuntimeError("Application account connected to an unexpected database")
        finally:
            app_connection.close()

        print("SUCCESS: QueueLess MySQL database and account are configured and verified.")
        return 0
    except Exception as exc:
        # Driver errors do not include the supplied password; avoid printing
        # configuration values or full connection objects in setup output.
        error_code = getattr(exc, "args", (None,))[0]
        print(f"MySQL setup failed (error {error_code}). Check server availability and .env settings.", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())

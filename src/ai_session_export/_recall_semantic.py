"""Run recall's pinned semantic-search commands with SQLite diagnostics."""

import json
import sqlite3
import sys

from semantic_search_skill import cli
from semantic_search_skill.index import DB_NAME, CacheError


def main() -> int:
    parser = cli.build_parser()
    args = parser.parse_args()
    commands = {"query": cli.run_query, "rebuild": cli.run_rebuild}
    if args.command not in commands:
        parser.error("recall supports query and rebuild")
    try:
        # The parent supplies the model, endpoint and environment. Skip dotenv discovery.
        return commands[args.command](args)
    except (CacheError, sqlite3.Error) as exc:
        # The pinned CLI prints only the outer CacheError when reading metadata fails.
        cause = exc.__cause__ if isinstance(exc, CacheError) else exc
        message = str(exc)
        if isinstance(cause, sqlite3.Error):
            db = cli.cache_dir_from_args(args) / DB_NAME
            code = getattr(cause, "sqlite_errorname", None) or type(cause).__name__
            message = f"{db}: {code}: {cause}"
            if code.startswith(("SQLITE_CANTOPEN", "SQLITE_READONLY", "SQLITE_PERM")):
                message += ". Check the local --cache-dir path and write access for SQLite WAL sidecar files."
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

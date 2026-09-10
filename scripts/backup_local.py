"""Create, verify, or restore a portable local Content OS backup."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.backup import BackupError, create_backup, restore_backup, verify_backup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="create a consistent local backup")
    create.add_argument("--data-root", type=Path, default=Path("content-os-data"))
    create.add_argument("--database", type=Path)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--overwrite", action="store_true", help="replace the explicitly named existing archive")

    verify = commands.add_parser("verify", help="verify archive hashes")
    verify.add_argument("--archive", type=Path, required=True)

    restore = commands.add_parser("restore", help="restore into a new directory")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--target-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create":
            database = args.database or args.data_root / "content-os.sqlite3"
            result = create_backup(args.data_root, database, args.output, overwrite=args.overwrite)
        elif args.command == "verify":
            result = verify_backup(args.archive)
        else:
            result = restore_backup(args.archive, args.target_dir)
    except (BackupError, OSError) as exc:
        print(f"backup error: {exc}", file=sys.stderr)
        return 2
    # Keep command output ASCII-safe for the default Windows console code page;
    # JSON consumers decode escaped local filenames back to their real paths.
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Copy the supervisor's contract for the bot into the tests (docs/PLAN-front-end.md, *Tests and the fake
supervisor*): ``python scripts/sync_contract.py [path to the supervisor's checkout]``. Run it whenever the
supervisor's ``docs/api/openapi-bot.json`` changes, then make the fake supervisor and the client follow."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT.parent / "arma-server-supervisior"
TARGET = ROOT / "tests" / "contract" / "openapi-bot.json"


def main(argv: list[str]) -> int:
    source = Path(argv[0]) if argv else DEFAULT
    contract = source / "docs" / "api" / "openapi-bot.json"
    if not contract.is_file():
        print(f"No contract at {contract}", file=sys.stderr)
        return 1
    if TARGET.is_file() and TARGET.read_bytes() == contract.read_bytes():
        print(f"{TARGET.relative_to(ROOT)} is current")
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(contract, TARGET)
    print(f"Copied {contract} to {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

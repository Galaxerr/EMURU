from __future__ import annotations

import argparse
import json
from pathlib import Path

from emuru.vault.indexer import index_vault


def main() -> None:
    parser = argparse.ArgumentParser(description=("Build EMURU vault indexes."))

    parser.add_argument(
        "--vault",
        required=True,
        type=Path,
        help="Path to EMURU-vault",
    )

    args = parser.parse_args()

    result = index_vault(args.vault)

    print(
        json.dumps(
            result,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

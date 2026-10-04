from __future__ import annotations

import os
from pathlib import Path

from emuru.vault_mcp import create_mcp


def _vault_path() -> Path:
    value = os.environ.get("EMURU_VAULT_PATH")

    if not value:
        raise RuntimeError("EMURU_VAULT_PATH is not set.")

    return Path(value)


mcp = create_mcp(_vault_path())


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

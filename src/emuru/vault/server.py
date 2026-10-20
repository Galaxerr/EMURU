from __future__ import annotations

import os
from pathlib import Path

from emuru.vault.mcp import create_mcp


def _vault_path() -> Path:
    value = os.environ.get("EMURU_VAULT_PATH")

    if not value:
        raise RuntimeError("EMURU_VAULT_PATH is not set.")

    return Path(value)


proposal_home = os.environ.get("EMURU_MUTATION_PROFILE_HOME")
mcp = create_mcp(
    _vault_path(), proposal_home=Path(proposal_home) if proposal_home else None
)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

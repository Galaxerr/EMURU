"""CLI transport for the shared Hermes vault profile module."""

import argparse
import json
import subprocess
from pathlib import Path

from emuru import hermes_profile as profile

ROOT = Path(__file__).resolve().parents[1]


class CLIConfig:
    """Native Hermes config commands; policy stays in the profile module."""

    def __init__(self, root):
        self.launcher = str(root / "scripts/hermes-emuru.sh")

    def get(self, key):
        try:
            response = subprocess.run(
                [self.launcher, "config", "get", key, "--json"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.CalledProcessError:
            raise profile.ProfileError(
                f"EMURU live configuration unavailable: {key}"
            ) from None
        return json.loads(response.stdout)

    def set(self, key, value, force=False):
        command = [self.launcher, "config", "set"]
        if force:
            command.append("--force")
        command.extend([key, value if isinstance(value, str) else json.dumps(value)])
        subprocess.run(command, check=True, timeout=30)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Validate version-controlled settings without launching Hermes",
    )
    args = parser.parse_args()
    if args.apply and args.offline:
        parser.error("--apply and --offline cannot be combined")
    try:
        if args.offline:
            profile.expected_settings(ROOT)
            print("EMURU provider and vault policy configuration: PASS (offline)")
            return
        config = CLIConfig(ROOT)
        profile.configure_profile(ROOT, config.get, config.set if args.apply else None)
    except profile.ProfileError as error:
        raise SystemExit(str(error)) from None
    print("EMURU provider and vault profile configuration: PASS")


if __name__ == "__main__":
    main()

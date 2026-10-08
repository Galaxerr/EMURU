"""List, select or test models available on your Ollama connection."""

import argparse
import json
import os
from pathlib import Path

from emuru.environment import load_env
from emuru.models.ollama import DEFAULT_BASE_URL, OllamaClient, OllamaConnection

ROOT = Path(__file__).resolve().parents[2]


def choose_model(models: list[dict], requested: str | None = None) -> str:
    names = list(dict.fromkeys(item["name"] for item in models))
    if not names:
        raise ValueError(
            "No Ollama models available. Add a model with 'ollama pull MODEL' first."
        )
    if requested:
        if requested not in names:
            raise ValueError(
                "Model is not available on this machine; use --list to see available models"
            )
        return requested
    for index, name in enumerate(names, 1):
        print(f"{index}. {name}")
    try:
        answer = input(
            "Choose a model number or exact name (blank to cancel): "
        ).strip()
    except (EOFError, KeyboardInterrupt) as error:
        raise ValueError("Model selection cancelled") from error
    if answer in names:
        return answer
    if answer.isdecimal() and 1 <= int(answer) <= len(names):
        return names[int(answer) - 1]
    raise ValueError(
        "Model selection cancelled"
        if not answer
        else "Choose a number or name from the displayed list"
    )


def main():
    load_env(Path(__file__).resolve().parents[2] / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true", help="Run chat and tool calls")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--list", action="store_true", help="List every model available on this PC"
    )
    actions.add_argument(
        "--select",
        nargs="?",
        const="",
        metavar="MODEL",
        help="Choose interactively, or save an exact model name from --list",
    )
    actions.add_argument(
        "--model", help="Test a model without changing the saved selection"
    )
    parser.add_argument("--route", default=os.environ.get("EMURU_CONTAINER_ROUTE"))
    parser.add_argument("--primary")
    parser.add_argument("--local-candidate")
    parser.add_argument(
        "--provider", choices=("ollama", "gemini", "openai-api"), default="ollama"
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args()
    if args.primary or args.local_candidate:
        if (
            not (args.primary and args.local_candidate and args.route)
            or args.select is not None
            or args.model
            or args.smoke
            or args.list
        ):
            parser.error(
                "Pair selection requires --route, --primary and --local-candidate only"
            )
        from emuru.models.gateway import select_pair, write_private

        try:
            route = select_pair(
                args.primary, args.local_candidate, args.base_url, args.provider
            )
            write_private(args.route, json.dumps(route, indent=2) + "\n")
        except (ValueError, RuntimeError, OSError) as error:
            raise SystemExit(str(error)) from None
        print(
            "Private primary/local-candidate selection saved; qualification remains null"
        )
        return
    if args.route and not args.list:
        parser.error(
            "Private route requires pair selection or --list; legacy selection remains separate"
        )
    if args.provider != "ollama":
        parser.error("--provider applies only to pair selection")
    if args.list and args.smoke:
        parser.error("--list cannot be combined with --smoke")
    path = ROOT / "infra/hermes/model-selection.json"
    selection = json.loads(path.read_text())
    base_url = (
        selection.get("base_url", DEFAULT_BASE_URL)
        if selection.get("provider") == "ollama"
        else DEFAULT_BASE_URL
    )
    try:
        if args.list or args.select is not None:
            # The PC's list is served by its daemon, not the public cloud catalog.
            if base_url.rstrip("/") in {"https://ollama.com", "https://ollama.com/v1"}:
                base_url = DEFAULT_BASE_URL
            client = OllamaClient(args.base_url if args.route else base_url)
            models = client.list_models()
            if args.list:
                print(
                    "\n".join(item["name"] for item in models)
                    if models
                    else "No Ollama models available. Add one with 'ollama pull MODEL'."
                )
                return
            model = choose_model(models, args.select)
            connection = OllamaConnection(model, base_url)
            if args.smoke:
                connection.smoke()
            path.write_text(
                json.dumps(
                    {"provider": "ollama", "model": model, "base_url": client.base_url},
                    indent=2,
                )
                + "\n"
            )
            print(f"Selected Ollama model: {model}")
            print(
                "Apply it with: uv run python scripts/hermes/vault-profile.py --apply"
            )
            return
        if selection.get("provider") != "ollama":
            raise ValueError("Use --select to choose an Ollama model first")
        connection = OllamaConnection(args.model or selection["model"], base_url)
        if args.smoke:
            connection.smoke()
        else:
            connection.check()
    except (ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from error
    print(
        f"Ollama connection{' / chat / tools' if args.smoke else ''}: PASS ({connection.model})"
    )


if __name__ == "__main__":
    main()

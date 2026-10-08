"""Disposable v0.3.2 acceptance: no live bot, credentials, cloud calls or weight pulls."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def fixture():
    class Handler(BaseHTTPRequestHandler):
        attempts = 0

        def log_message(self, *args):
            pass

        def reply(self, data, status=200):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        def do_GET(self):
            self.reply({"attempts": Handler.attempts})

        def do_POST(self):
            assert self.headers.get("Authorization") == "Bearer synthetic-ollama-key"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path != "/api/chat":
                return self.reply({"error": "native chat required"}, 400)
            Handler.attempts += 1
            assert body["model"] == "synthetic-cloud"
            assert body.get("stream") is False
            messages = body["messages"]
            if messages[-1].get("content") == "fail":
                return self.reply({"error": "synthetic unavailable"}, 503)
            message = {"role": "assistant", "content": ""}
            if any(item["role"] == "tool" for item in messages):
                assert any(
                    "SYNTHETIC_TOOL_OK" in item.get("content", "")
                    for item in messages
                    if item["role"] == "tool"
                )
                message["content"] = "SYNTHETIC_TOOL_OK"
            else:
                assert body["tools"][0]["function"]["name"] == "connection_probe"
                message["tool_calls"] = [
                    {
                        "function": {
                            "name": "connection_probe",
                            "arguments": {"message": "ping"},
                        }
                    }
                ]
            self.reply(
                {
                    "model": body["model"],
                    "message": message,
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 1,
                    "eval_count": 1,
                }
            )

    ThreadingHTTPServer(("0.0.0.0", 11434), Handler).serve_forever()


def seed():
    import yaml

    from emuru.hermes import profile
    from emuru.telegram.queue import TelegramQueue, load_telegram_settings

    root = Path("/opt/emuru")
    home = profile.profile_home()
    config = {"model": {}, "mcp_servers": {}}

    def get(key):
        cursor = config
        for part in key.split("."):
            cursor = cursor.get(part) if isinstance(cursor, dict) else None
        return cursor

    def set_value(key, value, force=False):
        cursor = config
        parts = key.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value

    profile.configure_profile(root, get, set_value)
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    shutil.copyfile(root / "agents/emuru/SOUL.md", home / "SOUL.md")
    (home / "synthetic-history.json").write_text('{"synthetic_history": true}\n')
    queue = TelegramQueue(
        home / "emuru-telegram/queue.sqlite3",
        load_telegram_settings(root / "infra/hermes/telegram-settings.json"),
        42,
        clock=lambda: 100000,
    )
    try:
        update = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 100000,
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "is_bot": False},
                "text": "disposable persistence check",
            },
        }
        assert queue.stage(7, [update]) == ["queued"]
        assert queue.status() == {"queued": 1}
    finally:
        queue.close()


def snapshot():
    from emuru.hermes import profile

    home = profile.profile_home()
    database = home / "emuru-telegram/queue.sqlite3"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute("SELECT * FROM updates ORDER BY sequence").fetchall()
    assert rows and len(rows) == 1
    payload = {
        "receipts": rows,
        "files": {
            name: hashlib.sha256((home / name).read_bytes()).hexdigest()
            for name in ("synthetic-history.json", "config.yaml", "SOUL.md")
        },
    }
    print(hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest())


def native_inference():
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from openai import OpenAI

    runtime = resolve_runtime_provider(requested="custom:emuru", target_model="emuru")
    assert runtime["base_url"] == "http://litellm:4000/v1"
    assert runtime["api_key"] == os.environ["EMURU_GATEWAY_KEY"]
    client = OpenAI(
        base_url=runtime["base_url"], api_key=runtime["api_key"], max_retries=0
    )
    messages = [{"role": "user", "content": "probe"}]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "connection_probe",
                "parameters": {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
            },
        }
    ]
    first = client.chat.completions.create(
        model="emuru", messages=messages, tools=tools
    )
    message = first.choices[0].message
    assert len(message.tool_calls) == 1
    call = message.tool_calls[0]
    assert call.id and call.function.name == "connection_probe"
    assert json.loads(call.function.arguments) == {"message": "ping"}
    messages += [
        message.model_dump(exclude_none=True),
        {"role": "tool", "tool_call_id": call.id, "content": "SYNTHETIC_TOOL_OK"},
    ]
    second = client.chat.completions.create(
        model="emuru", messages=messages, tools=tools
    )
    assert second.choices[0].message.content == "SYNTHETIC_TOOL_OK"
    print("Pinned Hermes provider / native tool round trip: PASS")


def verify(config_only=False):
    import yaml

    from emuru.models.gateway import render, write_private

    root = Path(__file__).resolve().parents[2]
    private = Path(tempfile.mkdtemp(prefix="emuru-container-"))
    os.umask(0o077)
    for folder in ("profiles/emuru", "state", "runtime", "ollama"):
        (private / folder).mkdir(parents=True, mode=0o700)
    key = "sk-" + os.urandom(24).hex()
    write_private(private / "gateway.key", key)
    write_private(private / "ollama-api.key", "synthetic-ollama-key")
    write_private(private / "gemini-api.key", "")
    write_private(private / "openai-api.key", "")
    route = {
        "schema_version": 1,
        "primary": {"provider": "ollama", "model": "synthetic-cloud"},
        "fallback": {
            "provider": "ollama",
            "model": "synthetic-local",
            "digest": "a" * 64,
            "context_tokens": 65536,
            "output_reserve_tokens": 4096,
            "qualification": None,
        },
    }
    write_private(private / "route.json", json.dumps(route))
    config = yaml.safe_load(render(route))
    config["model_list"][0]["litellm_params"]["api_base"] = "http://ollama:11434"
    write_private(private / "litellm.yaml", yaml.safe_dump(config, sort_keys=True))
    values = {
        "EMURU_UID": os.getuid(),
        "EMURU_GID": os.getgid(),
        "EMURU_TELEGRAM_OWNER_ID": 42,
        "EMURU_PROFILE": private / "profiles/emuru",
        "EMURU_STATE": private / "state",
        "EMURU_RUNTIME": private / "runtime",
        "EMURU_ROUTE": private / "route.json",
        "EMURU_GATEWAY_CONFIG": private / "litellm.yaml",
        "EMURU_GATEWAY_KEY_FILE": private / "gateway.key",
        "EMURU_OLLAMA_API_KEY_FILE": private / "ollama-api.key",
        "EMURU_GEMINI_API_KEY_FILE": private / "gemini-api.key",
        "EMURU_OPENAI_API_KEY_FILE": private / "openai-api.key",
        "EMURU_OLLAMA_STATE": private / "ollama",
    }
    write_private(
        private / "deployment.env",
        "".join(f"{key}={value}\n" for key, value in values.items()),
    )
    command = [
        "docker",
        "compose",
        "--project-name",
        private.name,
        "--env-file",
        str(private / "deployment.env"),
        "-f",
        str(root / "infra/docker/compose.yaml"),
    ]

    def compose(*args, extra=()):
        result = subprocess.run(
            [*command, *extra, *args], check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

    compose("config", "--quiet")
    print("Compose private-path configuration: PASS", flush=True)
    if config_only:
        return
    mount = {
        "type": "bind",
        "source": str(Path(__file__).resolve()),
        "target": "/check.py",
        "read_only": True,
        "bind": {"create_host_path": False},
    }
    override = {
        "services": {
            "emuru": {"volumes": [mount]},
            "litellm": {"ports": ["127.0.0.1::4000"]},
        }
    }
    write_private(private / "check.yaml", yaml.safe_dump(override))
    extra = ("-f", str(private / "check.yaml"))

    def app(*args):
        return compose("run", "--rm", "--no-deps", "emuru", *args, extra=extra)

    try:
        compose(
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            "180",
            "--pull",
            "never",
            "--no-build",
            "ollama",
            "litellm",
            extra=extra,
        )
        before = compose("exec", "-T", "ollama", "ollama", "list")
        cache_identity = compose(
            "exec", "-T", "ollama", "sha256sum", "/root/.ollama/id_ed25519.pub"
        )
        compose(
            "up",
            "-d",
            "--wait",
            "--force-recreate",
            "--pull",
            "never",
            "--no-build",
            "ollama",
            "litellm",
            extra=extra,
        )
        assert compose("exec", "-T", "ollama", "ollama", "list") == before
        assert (
            compose("exec", "-T", "ollama", "sha256sum", "/root/.ollama/id_ed25519.pub")
            == cache_identity
        )
        print(
            "Ollama cache identity recreation: PASS (empty disposable inventory; model-digest persistence pending)",
            flush=True,
        )
        for args in (
            (
                "uv",
                "run",
                "--frozen",
                "--no-sync",
                "python",
                "scripts/hermes/vault.py",
                "prepare",
            ),
            (
                "uv",
                "run",
                "--frozen",
                "--no-sync",
                "python",
                "scripts/hermes/vault.py",
                "check",
            ),
            ("scripts/hermes/telegram.sh", "--runtime-check"),
            (
                "uv",
                "run",
                "--frozen",
                "--no-sync",
                "python",
                "scripts/hermes/vault-profile.py",
                "--offline",
            ),
            ("scripts/hermes/telegram.sh", "--offline"),
        ):
            print(app(*args), flush=True)
        app("/opt/emuru/.venv/bin/python", "/check.py", "seed")
        print(
            app(
                "uv",
                "run",
                "--frozen",
                "--no-sync",
                "python",
                "scripts/hermes/vault-profile.py",
                "--apply",
            ),
            flush=True,
        )
        first = app("/opt/emuru/.venv/bin/python", "/check.py", "snapshot")
        assert app("/opt/emuru/.venv/bin/python", "/check.py", "snapshot") == first
        print(app("scripts/hermes/telegram.sh", "--status"), flush=True)
        print(
            "Profile/history and queued receipt survive application recreation: PASS",
            flush=True,
        )
        compose("stop", extra=extra)
        override["services"]["ollama"] = {
            "image": "emuru:0.3.2",
            "entrypoint": ["/usr/local/bin/python", "/check.py", "fixture"],
            "volumes": [mount],
            "healthcheck": {
                "test": [
                    "CMD",
                    "python",
                    "-c",
                    "import urllib.request; urllib.request.urlopen('http://localhost:11434/stats')",
                ]
            },
        }
        write_private(private / "check.yaml", yaml.safe_dump(override))
        compose(
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            "180",
            "--pull",
            "never",
            "--no-build",
            "ollama",
            "litellm",
            extra=extra,
        )
        address = "http://" + compose("port", "litellm", "4000", extra=extra)

        def attempts():
            return json.loads(
                compose(
                    "exec",
                    "-T",
                    "ollama",
                    "python",
                    "-c",
                    "import urllib.request; print(urllib.request.urlopen('http://localhost:11434/stats').read().decode())",
                    extra=extra,
                )
            )["attempts"]

        def request(token, model="emuru", content="fail"):
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = "Bearer " + token
            try:
                with urlopen(
                    Request(
                        address + "/v1/chat/completions",
                        json.dumps(
                            {
                                "model": model,
                                "messages": [{"role": "user", "content": content}],
                            }
                        ).encode(),
                        headers,
                    ),
                    timeout=30,
                ) as response:
                    return response.status
            except HTTPError as error:
                return error.code

        assert request(None) in (401, 403)
        # This pinned DB-free proxy rejects non-master keys with HTTP 400 (no_db_connection).
        assert request("sk-wrong") in (400, 401, 403)
        assert request(key, model="unknown") in (400, 404)
        assert attempts() == 0
        print(app("/opt/hermes/.venv/bin/python", "/check.py", "native"), flush=True)
        assert attempts() == 2
        assert request(key) >= 400
        assert attempts() == 3
        assert key not in compose("logs", "--no-color", extra=extra)
        print(
            "Gateway auth / alias rejection / one attempt per request / secret-free logs: PASS",
            flush=True,
        )
    finally:
        compose("down", "--remove-orphans", extra=extra)
        print(
            "Disposable containers stopped; private evidence directory: "
            + str(private),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        nargs="?",
        choices=("verify", "fixture", "seed", "snapshot", "native"),
        default="verify",
    )
    parser.add_argument("--config-only", action="store_true")
    args = parser.parse_args()
    if Path("/opt/emuru/src").is_dir():
        sys.path.insert(0, "/opt/emuru/src")
    {
        "verify": lambda: verify(args.config_only),
        "fixture": fixture,
        "seed": seed,
        "snapshot": snapshot,
        "native": native_inference,
    }[args.mode]()

"""Redacted gateway evidence in container logs; status never probes inference."""

import hashlib
import json
import socket
import time

PREFIX = "EMURU_ROUTE_OBSERVATION "
FRESH_SECONDS = 300
STATES = {"READY", "LOCAL_UNQUALIFIED", "DEGRADED_PRIMARY", "UNAVAILABLE", "UNKNOWN"}


def fingerprint(route):
    return hashlib.sha256(json.dumps(route, sort_keys=True).encode()).hexdigest()


def emit(route, state, used=None):
    try:
        print(
            PREFIX
            + json.dumps(
                {
                    "at": time.time(),
                    "instance": socket.gethostname(),
                    "route": fingerprint(route),
                    "state": state,
                    "used": used,
                }
            ),
            flush=True,
        )
    except OSError:
        pass  # Missing diagnostics must never retry a completed inference request.


def read(logs, route, instance, started, *, now=None):
    """Only evidence for this configuration and this container incarnation counts."""
    now = time.time() if now is None else now
    current, last = None, None
    for line in logs.splitlines():
        if not line.startswith(PREFIX):
            continue
        try:
            record = json.loads(line[len(PREFIX) :])
            if (
                set(record) != {"at", "instance", "route", "state", "used"}
                or record["instance"] != instance
                or record["route"] != fingerprint(route)
                or type(record["at"]) not in (int, float)
                or not 0 <= record["at"] <= now
                or record["state"] not in STATES
                or record["used"] not in (None, "primary", "local")
            ):
                continue
            if record["at"] >= started and (
                current is None or record["at"] >= current["at"]
            ):
                current = record
            if record["used"] and (last is None or record["at"] >= last["at"]):
                last = record
        except (ValueError, TypeError, KeyError):
            continue
    return {
        "state": (
            current["state"]
            if current and now - current["at"] <= FRESH_SECONDS
            else "UNKNOWN"
        ),
        "observed_at": current["at"] if current else None,
        "last_used": {"route": last["used"], "at": last["at"]} if last else None,
    }

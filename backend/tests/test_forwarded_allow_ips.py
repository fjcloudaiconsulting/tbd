"""INF-01: one setting, ``FORWARDED_ALLOW_IPS``, names the peers whose forwarded headers are believed.

uvicorn reads it itself: with no ``--forwarded-allow-ips`` on the command line, its
``Config`` falls back to the ``FORWARDED_ALLOW_IPS`` environment variable. The backend and
MCP images set today's list as that variable's default and pass no flag, so a deployment
narrows it with an env var instead of repeating the image command. ``rate_limit`` walks
``X-Forwarded-For`` itself; it derives its trusted peers from the same variable
(``Settings.forwarded_allow_ips``, same default), so the two cannot drift.

Wrong implementations these kill: the flag left on an image or compose command (the variable
is then ignored), an image default that differs from the settings default, a rate limiter
that keeps its own wider list, and a malformed value that boots and trusts nothing (or
everything) without a word.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import uvicorn
from pydantic import ValidationError
from starlette.requests import Request
from uvicorn.main import main as uvicorn_cli
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import rate_limit
from app.config import Settings, settings

BACKEND = Path(__file__).resolve().parents[1]

# The value both images hard-coded in --forwarded-allow-ips before INF-01.
TODAY = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7,127.0.0.1,::1"
CLOUD_INFRA = "10.42.0.0/16"  # what cloud-infra narrows it to: the pod network


def _repo_file(name: str) -> Path:
    """A repo-root file on a checkout, or its read-only mount at /app in the dev
    container (docker-compose.yml). Raises rather than skips."""
    for candidate in (BACKEND.parent / name, Path("/app") / name):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"{name} not found (the dev container needs ./{name}:/app/{name}:ro)")


def _stage(dockerfile: str) -> str:
    """The serving stage: ``prod`` in the backend Dockerfile, the only stage in Dockerfile.mcp."""
    text = (BACKEND / dockerfile).read_text()
    return text.split("\nFROM runtime AS prod\n", 1)[1] if dockerfile == "Dockerfile" else text


def _cmd(dockerfile: str) -> list[str]:
    (line,) = [ln for ln in _stage(dockerfile).splitlines() if ln.startswith("CMD ")]
    return json.loads(line[len("CMD "):])


def _env_default(dockerfile: str) -> str:
    found = re.findall(r"^ENV FORWARDED_ALLOW_IPS=(\S+)$", _stage(dockerfile), re.MULTILINE)
    assert len(found) == 1, f"{dockerfile}: expected one ENV FORWARDED_ALLOW_IPS in the serving stage, found {found}"
    return found[0]


def _uvicorn_trust(cmd: list[str]) -> str:
    """What uvicorn trusts when it runs ``cmd``: its CLI parse, then its Config fallback."""
    assert cmd[0] == "uvicorn", cmd
    params = uvicorn_cli.make_context("uvicorn", cmd[1:]).params
    assert params["proxy_headers"] is True, cmd
    config = uvicorn.Config(cmd[1], forwarded_allow_ips=params["forwarded_allow_ips"])
    return config.forwarded_allow_ips


# ── the images and compose ───────────────────────────────────────────────


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "Dockerfile.mcp"])
def test_image_sets_the_default_and_passes_no_flag(dockerfile):
    cmd = _cmd(dockerfile)
    assert "--proxy-headers" in cmd, cmd
    assert not any(a.startswith("--forwarded-allow-ips") for a in cmd), (
        f"{dockerfile}: a --forwarded-allow-ips flag makes uvicorn ignore FORWARDED_ALLOW_IPS: {cmd}"
    )
    assert _env_default(dockerfile) == TODAY
    assert Settings.model_fields["forwarded_allow_ips"].default == TODAY


def test_migrations_image_does_not_carry_the_variable():
    text = (BACKEND / "Dockerfile").read_text()
    migrations = text.split("\nFROM runtime AS migrations\n", 1)[1].split("\nFROM runtime AS prod\n", 1)[0]
    runtime = text.split(" AS runtime\n", 1)[1].split("\nFROM runtime AS migrations\n", 1)[0]
    assert "FORWARDED_ALLOW_IPS" not in runtime + migrations


@pytest.mark.parametrize("compose", ["docker-compose.yml", "docker-compose.prod.yml"])
def test_compose_commands_pass_no_flag(compose):
    """The compose services run the same images; a flag on their command would make a
    FORWARDED_ALLOW_IPS in .env narrow the rate limiter but not uvicorn."""
    commands = re.findall(r'^\s*command: (\[.*\])$', _repo_file(compose).read_text(), re.MULTILINE)
    uvicorn_cmds = [json.loads(c) for c in commands if '"uvicorn"' in c]
    assert uvicorn_cmds, f"{compose}: no uvicorn command found; the parser is stale"
    for cmd in uvicorn_cmds:
        assert not any(a.startswith("--forwarded-allow-ips") for a in cmd), f"{compose}: {cmd}"


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "Dockerfile.mcp"])
def test_uvicorn_reads_the_variable_under_the_image_command(dockerfile, monkeypatch):
    """Confirms, on the installed uvicorn, that the image command leaves the trust list to
    FORWARDED_ALLOW_IPS: the image default when only the ENV is set, the override when set."""
    cmd = _cmd(dockerfile)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", _env_default(dockerfile))
    assert _uvicorn_trust(cmd) == TODAY
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", CLOUD_INFRA)
    assert _uvicorn_trust(cmd) == CLOUD_INFRA


# ── the setting ───────────────────────────────────────────────────────────


def test_default_matches_today(monkeypatch):
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    s = Settings()
    assert s.forwarded_allow_ips == TODAY
    assert s.forwarded_allow_networks == tuple(ipaddress.ip_network(e) for e in TODAY.split(","))
    # rate_limit uses the setting, not a list of its own.
    assert rate_limit._TRUSTED_PROXY_NETWORKS == settings.forwarded_allow_networks
    for peer in ("10.1.2.3", "172.20.0.5", "192.168.65.1", "fd00::5", "127.0.0.1", "::1"):
        assert rate_limit._is_trusted_proxy(peer), peer
    for peer in ("203.0.113.9", "8.8.8.8", "172.32.0.1", "11.0.0.1", "2001:db8::1", "", None):
        assert not rate_limit._is_trusted_proxy(peer), peer


def test_entries_are_trimmed_and_empty_ones_ignored(monkeypatch):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", " 10.42.0.0/16 , ,127.0.0.1,")
    assert Settings().forwarded_allow_networks == (
        ipaddress.ip_network("10.42.0.0/16"),
        ipaddress.ip_network("127.0.0.1/32"),
    )
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "")
    assert Settings().forwarded_allow_networks == ()


@pytest.mark.parametrize(
    "value",
    ["*", "10.42.0.1/16", "10.42.0.0/33", "not-an-ip", "10.0.0.0/8;192.168.0.0/16", "unix:/tmp/uvicorn.sock"],
)
def test_malformed_value_is_refused(monkeypatch, value):
    """uvicorn turns these into literals that match no peer (or, for "*", trusts every
    peer) without a word; the setting refuses them instead."""
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", value)
    with pytest.raises(ValidationError) as exc:
        Settings()
    assert [e["loc"] for e in exc.value.errors()] == [("forwarded_allow_ips",)]
    assert "FORWARDED_ALLOW_IPS" in str(exc.value)


def test_malformed_value_stops_the_process_at_startup():
    env = {**os.environ, "FORWARDED_ALLOW_IPS": "10.42.0.1/16"}
    r = subprocess.run([sys.executable, "-c", "import app.config"], cwd=BACKEND,
                       capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode != 0
    assert "invalid settings: forwarded_allow_ips" in r.stderr, r.stderr


# ── both layers, one value ────────────────────────────────────────────────


async def _resolve(trusted: str, peer: str, headers: dict[str, str]) -> tuple[str, str, str]:
    """Run a request from ``peer`` through uvicorn's proxy middleware, configured the way
    the image command configures it, into ``rate_limit.get_client_ip``."""
    seen: dict = {}

    async def app(scope, receive, send):
        seen["client"] = scope["client"][0]
        seen["scheme"] = scope["scheme"]
        seen["ip"] = rate_limit.get_client_ip(Request(scope))

    scope = {
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http", "path": "/",
        "raw_path": b"/", "query_string": b"", "server": ("backend", 8000), "client": (peer, 40000),
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    await ProxyHeadersMiddleware(app, trusted_hosts=trusted)(scope, None, None)
    return seen["client"], seen["scheme"], seen["ip"]


FORWARDED = {"X-Forwarded-For": "198.51.100.7", "X-Forwarded-Proto": "https"}


@pytest.mark.parametrize(
    "value,peer,expected",
    [
        # Today's default: the private ranges and loopback are proxies.
        (TODAY, "172.20.0.5", ("198.51.100.7", "https", "198.51.100.7")),
        (TODAY, "10.42.3.4", ("198.51.100.7", "https", "198.51.100.7")),
        (TODAY, "203.0.113.9", ("203.0.113.9", "http", "203.0.113.9")),
        # Narrowed to the pod network: a private peer outside it is now a client in
        # BOTH layers. Before INF-01 the rate limiter still walked its X-Forwarded-For.
        (CLOUD_INFRA, "10.42.3.4", ("198.51.100.7", "https", "198.51.100.7")),
        (CLOUD_INFRA, "172.20.0.5", ("172.20.0.5", "http", "172.20.0.5")),
        (CLOUD_INFRA, "203.0.113.9", ("203.0.113.9", "http", "203.0.113.9")),
    ],
)
async def test_an_override_narrows_uvicorn_and_the_rate_limiter_together(monkeypatch, value, peer, expected):
    monkeypatch.delenv("CLIENT_IP_HEADER", raising=False)
    monkeypatch.delenv("PFV_RUNTIME", raising=False)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", value)
    monkeypatch.setattr(rate_limit, "_TRUSTED_PROXY_NETWORKS", Settings().forwarded_allow_networks)

    trusted = _uvicorn_trust(_cmd("Dockerfile"))

    assert trusted == value
    assert await _resolve(trusted, peer, FORWARDED) == expected

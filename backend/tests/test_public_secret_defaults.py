"""INF-03: production refuses every secret value committed to this public repository.

The fence half scans the files that carry dev, CI and test secrets and fails on any literal that
production would accept, so a new dev default cannot land without its refusal. Dev and test boots
keep accepting them. In the backend container the scanned repo-root files are read-only mounts
(docker-compose.yml); on a bare checkout they are where git put them.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.config import SECRET_FIELDS, Settings

BACKEND = Path(__file__).resolve().parents[1]


def _find_repo_root(start: Path) -> Path:
    """Walk upward to the directory holding .github/workflows/ci.yml: the checkout on a host,
    /app (where .github is mounted) in the backend container."""
    for candidate in [start, *start.parents]:
        if (candidate / ".github" / "workflows" / "ci.yml").exists():
            return candidate
    raise RuntimeError("Could not locate the repo root containing .github/workflows/ci.yml.")


REPO_ROOT = _find_repo_root(Path(__file__).resolve())

# Compose names that feed a secret setting of another name (docker-compose.prod.yml, mcp).
ALIASES = {
    "MCP_JWT_SECRET_KEY": "jwt_secret_key",
    "MCP_MFA_RECOVERY_HMAC_KEY": "mfa_recovery_hmac_key",
}
ENV_TO_FIELD = {name.upper(): name for name in SECRET_FIELDS} | ALIASES

# The release smoke's throwaway values (compose.smoke.yaml). It boots the published images with
# APP_ENV=production and the shared smoke workflow injects no secret, so production must accept
# these two until that workflow passes per-run values. Nothing else may join them.
RELEASE_SMOKE_EXCEPTIONS = {
    ("JWT_SECRET_KEY", "smoke-jwt-key-not-a-real-secret-0000000000"),
    ("API_TOKEN_HMAC_KEY", "smoke-pat-key-not-a-real-secret-11111111111"),
}

_VALID_JWT = "jwt-secret-key-at-least-32-characters-long-000"
_VALID_PAT = "dedicated-pat-hmac-key-distinct-and-32plus-chars"


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that reads Compose's !reset / !override merge tags as plain values."""


for _tag in ("!reset", "!override"):
    _ComposeLoader.add_constructor(
        _tag,
        lambda loader, node: (
            loader.construct_sequence(node)
            if isinstance(node, yaml.SequenceNode)
            else loader.construct_mapping(node)
            if isinstance(node, yaml.MappingNode)
            else loader.construct_scalar(node)
        ),
    )


def _literal(raw: object) -> str | None:
    """The value a variable gets when nothing else sets it, or None when it is not a committed
    literal: empty, required (``${X:?}``), passed through (``${X}``), generated at run time
    (``$(...)``) or a GitHub expression."""
    if raw is None:
        return None
    value = str(raw).strip().strip('"').strip("'")
    if not value or "$(" in value or "${{" in value:
        return None
    interpolation = re.fullmatch(r"\$\{\w+(?::?-(?P<default>[^}]*)|:?\?[^}]*)?\}", value)
    if interpolation:
        return interpolation.group("default") or None
    return None if "$" in value else value


def _environment_pairs(environment: object) -> list[tuple[str, object]]:
    if isinstance(environment, dict):
        return list(environment.items())
    if isinstance(environment, list):
        return [tuple(entry.split("=", 1)) for entry in environment if "=" in entry]
    return []


def _walk_env_blocks(node: object, keys: set[str]):
    """Every mapping value under one of ``keys`` (compose ``environment``, workflow ``env``)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in keys:
                yield value
            yield from _walk_env_blocks(value, keys)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_env_blocks(item, keys)


def _compose_literals(path: Path) -> set[tuple[str, str]]:
    document = yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader)
    found = set()
    for block in _walk_env_blocks(document, {"environment", "x-backend-env"}):
        for name, raw in _environment_pairs(block):
            if name.strip() in ENV_TO_FIELD and (value := _literal(raw)):
                found.add((name.strip(), value))
    return found


def _workflow_literals(path: Path) -> set[tuple[str, str]]:
    text = path.read_text(encoding="utf-8")
    found = set()
    for block in _walk_env_blocks(yaml.safe_load(text), {"env"}):
        for name, raw in _environment_pairs(block):
            if name in ENV_TO_FIELD and (value := _literal(raw)):
                found.add((name, value))
    # `docker run -e NAME=value` inside run scripts.
    for name, raw in re.findall(r"-e\s+(\w+)=(\S+)", text):
        if name in ENV_TO_FIELD and (value := _literal(raw)):
            found.add((name, value))
    return found


def _env_example_literals(path: Path) -> set[tuple[str, str]]:
    found = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"#?\s*(\w+)=(.*)", line.strip())
        if match and match.group(1) in ENV_TO_FIELD and (value := _literal(match.group(2))):
            found.add((match.group(1), value))
    return found


def _conftest_literals(path: Path) -> set[tuple[str, str]]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"setdefault", "setenv"}
            and len(node.args) >= 2
            and all(isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in node.args[:2])
            and node.args[0].value in ENV_TO_FIELD
            and node.args[1].value
        ):
            found.add((node.args[0].value, node.args[1].value))
    return found


SOURCES = {
    "docker-compose.yml": lambda: _compose_literals(REPO_ROOT / "docker-compose.yml"),
    "docker-compose.prod.yml": lambda: _compose_literals(REPO_ROOT / "docker-compose.prod.yml"),
    ".env.example": lambda: _env_example_literals(REPO_ROOT / ".env.example"),
    ".github/workflows": lambda: set().union(
        *(_workflow_literals(p) for p in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")))
    ),
    "tests/conftest.py": lambda: _conftest_literals(BACKEND / "tests" / "conftest.py"),
}


def _committed() -> list[tuple[str, str, str]]:
    return sorted((source, name, value) for source, scan in SOURCES.items() for name, value in scan())


def _production(**overrides) -> Settings:
    return Settings(
        **{
            "_env_file": None,
            "app_env": "production",
            "jwt_secret_key": _VALID_JWT,
            "api_token_hmac_key": _VALID_PAT,
            **overrides,
        }
    )


def test_the_scan_finds_the_known_dev_and_ci_literals():
    """Non-vacuous: each scanned file that carries one today still yields it."""
    found = {(source, name) for source, name, _ in _committed()}
    assert {
        ("docker-compose.yml", "API_TOKEN_HMAC_KEY"),
        ("docker-compose.yml", "JWT_SECRET_KEY"),
        ("docker-compose.yml", "MFA_RECOVERY_HMAC_KEY"),
        (".env.example", "JWT_SECRET_KEY"),
        (".github/workflows", "JWT_SECRET_KEY"),
        (".github/workflows", "MFA_RECOVERY_HMAC_KEY"),
        (".github/workflows", "API_TOKEN_HMAC_KEY"),
        ("tests/conftest.py", "JWT_SECRET_KEY"),
    } <= found


def _refused_in_production(field: str, value: str) -> bool:
    try:
        _production(**{field: value})
    except ValidationError:
        return True
    return False


# One test per property, looping over the literals: each failure message names the source.
def test_production_refuses_every_committed_secret():
    accepted = [
        (source, name)
        for source, name, value in _committed()
        if not _refused_in_production(ENV_TO_FIELD[name], value)
    ]
    assert not accepted, f"production boots with these committed secrets: {accepted}"


def test_dev_and_test_still_accept_them():
    rejected = []
    for source, name, value in _committed():
        if value == "change-me-generate-a-real-secret":
            continue  # the .env.example placeholder is refused in every environment already
        for env in ("development", "test"):
            try:
                Settings(_env_file=None, app_env=env, **{ENV_TO_FIELD[name]: value})
            except ValidationError:
                rejected.append((env, source, name))
    assert not rejected, f"dev or test no longer boots with: {rejected}"


def test_production_refuses_a_dev_only_value_in_any_secret_field():
    for field in SECRET_FIELDS:
        with pytest.raises(ValidationError, match=field.upper()):
            _production(**{field: "dev-only-" + "x" * 40})


def test_the_release_smoke_values_are_the_only_exception():
    smoke = _compose_literals(REPO_ROOT / "compose.smoke.yaml")
    assert smoke == RELEASE_SMOKE_EXCEPTIONS, (
        "compose.smoke.yaml changed its committed secrets. Production must refuse every committed "
        "value: add new ones to _PUBLIC_SECRET_VALUES and have the shared smoke workflow pass a "
        "per-run secret instead of widening this exception."
    )
    # The smoke boots the published images in production mode, so it must still pass.
    _production(**{ENV_TO_FIELD[name]: value for name, value in smoke})


def test_the_refusal_names_the_field_and_not_the_value():
    value = "dev-only-mcp-jwt-secret-never-the-backend-value"
    done = subprocess.run(
        [sys.executable, "-c", "import app.config"],
        cwd=BACKEND,
        env={
            "PATH": "/usr/bin:/bin",
            "APP_ENV": "production",
            "JWT_SECRET_KEY": value,
            "API_TOKEN_HMAC_KEY": _VALID_PAT,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode != 0
    assert "JWT_SECRET_KEY is a value committed to this public repository" in done.stderr
    assert value not in done.stdout + done.stderr

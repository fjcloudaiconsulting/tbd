"""INF-10: the dev stack listens on loopback only, and ``./tbd prod`` runs the immutable image.

Static fences on the two compose files (mounted read-only into the backend container, see
docker-compose.yml). A plain ``volumes: []`` in an override MERGES with the base file's mounts
instead of replacing them, so the prod rehearsal kept every dev bind mount (source, tests, .git)
and the dev dependency group. Compose's ``!reset`` tag (Compose 2.24+) is what clears them.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def _find_repo_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / ".github" / "workflows" / "ci.yml").exists():
            return candidate
    raise RuntimeError("Could not locate the repo root containing .github/workflows/ci.yml.")


REPO_ROOT = _find_repo_root(Path(__file__).resolve())


class Reset(list):
    """A sequence Compose's ``!reset`` tag replaces instead of merging."""


class _ComposeLoader(yaml.SafeLoader):
    pass


_ComposeLoader.add_constructor("!reset", lambda loader, node: Reset(loader.construct_sequence(node)))
_ComposeLoader.add_constructor(
    "!override", lambda loader, node: loader.construct_sequence(node)
)


def _services(name: str) -> dict:
    text = (REPO_ROOT / name).read_text(encoding="utf-8")
    return yaml.load(text, Loader=_ComposeLoader)["services"]


def test_every_dev_published_port_binds_loopback():
    published = {
        service: spec["ports"] for service, spec in _services("docker-compose.yml").items() if "ports" in spec
    }
    assert published, "nothing is published; the fence would be vacuous"
    for service, ports in published.items():
        for port in ports:
            assert str(port).startswith("127.0.0.1:"), (
                f"{service} publishes {port!r} on every interface: anyone on the same network "
                "reaches Swagger, the dev OAuth server and the demo superadmin."
            )


# mysql's data volume and nginx's config are not dev mounts; every other mount is.
KEPT_IN_PROD = {"mysql", "nginx"}


def test_the_prod_rehearsal_drops_every_dev_mount():
    base = _services("docker-compose.yml")
    prod = _services("docker-compose.prod.yml")
    mounted = sorted(
        service for service, spec in base.items() if spec.get("volumes") and service not in KEPT_IN_PROD
    )
    assert {"backend", "mcp", "frontend"} <= set(mounted)
    for service in mounted:
        volumes = prod[service].get("volumes")
        assert isinstance(volumes, Reset) and volumes == [], (
            f"docker-compose.prod.yml must clear {service}'s dev mounts with `volumes: !reset []`; "
            "a plain list merges with the base file's mounts."
        )


def test_the_prod_rehearsal_builds_without_the_dev_dependency_group():
    assert _services("docker-compose.yml")["backend"]["build"]["args"]["INSTALL_DEV"] == "true"
    assert _services("docker-compose.prod.yml")["backend"]["build"]["args"]["INSTALL_DEV"] == "false"

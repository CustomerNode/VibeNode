"""
Test that all Flask routes are documented in the OpenAPI spec.

Compares app.url_map against paths in docs/api/openapi.yaml.
Fails when a route exists in the app but is not documented.
"""

import re
from pathlib import Path

import pytest
import yaml


@pytest.fixture(scope="module")
def openapi_paths():
    """Load all documented paths from openapi.yaml."""
    spec_path = Path(__file__).resolve().parents[1] / "docs" / "api" / "openapi.yaml"
    if not spec_path.exists():
        pytest.skip("openapi.yaml not found")
    with open(spec_path, encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    paths = set()
    for path_str in spec.get("paths", {}):
        # Normalize OpenAPI {param} to Flask <param> for comparison
        normalized = re.sub(r"\{([^}]+)\}", r"<\1>", path_str)
        paths.add(normalized)
    return paths


@pytest.fixture(scope="module")
def flask_routes():
    """Get all registered routes from the Flask app."""
    from app import create_app
    app = create_app(testing=True)
    routes = set()
    for rule in app.url_map.iter_rules():
        # Skip static file serving and HEAD-only rules
        if rule.endpoint == "static":
            continue
        path = rule.rule
        routes.add(path)
    return routes


# Routes that are intentionally undocumented (internal Flask defaults, etc.)
IGNORED_ROUTES = {
    "/static/<path:filename>",
    "/api/docs/<path:filename>",  # Asset serving for the docs page itself
}


def test_all_routes_documented(flask_routes, openapi_paths):
    """Every Flask route should have a corresponding entry in openapi.yaml."""
    undocumented = []
    for route in sorted(flask_routes):
        if route in IGNORED_ROUTES:
            continue
        if route not in openapi_paths:
            undocumented.append(route)

    if undocumented:
        msg = (
            f"{len(undocumented)} route(s) missing from docs/api/openapi.yaml:\n"
            + "\n".join(f"  - {r}" for r in undocumented)
            + "\n\nAdd entries for these routes to keep the API docs in sync."
        )
        pytest.fail(msg)


def test_openapi_yaml_valid_syntax():
    """The openapi.yaml file should be valid YAML and have required top-level keys."""
    spec_path = Path(__file__).resolve().parents[1] / "docs" / "api" / "openapi.yaml"
    if not spec_path.exists():
        pytest.skip("openapi.yaml not found")
    with open(spec_path, encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    assert "openapi" in spec, "Missing 'openapi' version key"
    assert "info" in spec, "Missing 'info' section"
    assert "paths" in spec, "Missing 'paths' section"
    assert len(spec["paths"]) > 100, f"Expected 100+ paths, found {len(spec['paths'])}"

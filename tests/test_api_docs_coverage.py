"""
Test that all Flask routes are documented in the OpenAPI spec.

Compares app.url_map against paths and HTTP methods in docs/api/openapi.yaml.
Fails when a route or method exists in the app but is not documented.
"""

import re
from pathlib import Path

import pytest
import yaml


# Flask automatically adds HEAD and OPTIONS to every route — never check these.
AUTO_METHODS = {"HEAD", "OPTIONS"}


def _normalize_path(path_str):
    """Convert OpenAPI {param} syntax to Flask <param> syntax."""
    return re.sub(r"\{([^}]+)\}", r"<\1>", path_str)


@pytest.fixture(scope="module")
def openapi_spec():
    """Load the parsed openapi.yaml spec (raw dict)."""
    spec_path = Path(__file__).resolve().parents[1] / "docs" / "api" / "openapi.yaml"
    if not spec_path.exists():
        pytest.skip("openapi.yaml not found")
    with open(spec_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def openapi_paths(openapi_spec):
    """Set of normalized path strings documented in openapi.yaml."""
    return {_normalize_path(p) for p in openapi_spec.get("paths", {})}


@pytest.fixture(scope="module")
def openapi_methods(openapi_spec):
    """
    Dict mapping normalized path -> set of uppercase HTTP methods documented
    in openapi.yaml.  Only the five standard verbs are tracked.
    """
    tracked = {"get", "post", "put", "delete", "patch"}
    result = {}
    for path_str, path_item in openapi_spec.get("paths", {}).items():
        normalized = _normalize_path(path_str)
        result[normalized] = {m.upper() for m in path_item if m in tracked}
    return result


@pytest.fixture(scope="module")
def flask_routes():
    """Get all registered routes from the Flask app."""
    from app import create_app
    app = create_app(testing=True)
    routes = set()
    for rule in app.url_map.iter_rules():
        # Skip static file serving
        if rule.endpoint == "static":
            continue
        routes.add(rule.rule)
    return routes


@pytest.fixture(scope="module")
def flask_route_methods():
    """
    Dict mapping Flask path -> set of explicit HTTP methods (HEAD/OPTIONS excluded).
    """
    from app import create_app
    app = create_app(testing=True)
    result = {}
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        explicit = {m for m in rule.methods if m not in AUTO_METHODS}
        if explicit:
            result[rule.rule] = explicit
    return result


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


def test_all_methods_documented(flask_route_methods, openapi_methods):
    """
    Every HTTP method registered in Flask for a given route must also be
    documented in openapi.yaml for that path.

    HEAD and OPTIONS are excluded because Flask adds them automatically.
    Routes in IGNORED_ROUTES are skipped entirely.
    """
    missing = []
    for route in sorted(flask_route_methods):
        if route in IGNORED_ROUTES:
            continue
        flask_methods = flask_route_methods[route]
        documented_methods = openapi_methods.get(route, set())
        for method in sorted(flask_methods):
            if method not in documented_methods:
                missing.append(f"{method} {route}")

    if missing:
        msg = (
            f"{len(missing)} route method(s) not documented in docs/api/openapi.yaml:\n"
            + "\n".join(f"  - Route {entry} is not documented in openapi.yaml"
                        for entry in missing)
            + "\n\nAdd the missing method entries to keep the API docs in sync."
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

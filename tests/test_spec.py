"""MailClient and its models against docs/service-api.openapi.json.

Fails when the service API and the client drift apart: re-run
tools/service_api_docs.py, then fix whatever this reports.
"""

import inspect
import json
from dataclasses import MISSING, fields
from pathlib import Path

import pytest

from mail_serviceapi import MailClient, models
from mail_serviceapi.models import snake

SPEC = json.loads(
    (Path(__file__).parent.parent / "docs" / "service-api.openapi.json").read_text(
        encoding="utf-8"
    )
)
SCHEMAS = SPEC["components"]["schemas"]
SKIPPED = {"requestToken"}  # not a method: ClientCredentials, an httpx.Auth
PAGES = {"MailPage", "ChangePage"}  # hidden by the iterators
CONVENIENCE = {"close", "list_full_mails"}  # built on operations, not one


def deref(node: dict) -> dict:
    while "$ref" in node:
        section, name = node["$ref"].split("/")[-2:]
        node = SPEC["components"][section][name]
    return node


def operations() -> list[tuple[str, dict]]:
    return [
        (op["operationId"], op)
        for item in SPEC["paths"].values()
        for op in item.values()
        if op["operationId"] not in SKIPPED
    ]


def expected_parameters(op: dict) -> dict[str, bool]:
    """snake_case parameter name -> required, as the method should take it."""
    expected = {}
    for p in map(deref, op.get("parameters", [])):
        if p["name"] not in ("user", "cursor"):
            expected[snake(p["name"])] = p.get("required", False)
    body = op.get("requestBody", {}).get("content", {}).get("application/json")
    if body:
        schema = deref(body["schema"])
        for name in schema.get("properties", {}):
            expected[snake(name)] = name in schema.get("required", [])
    return expected


@pytest.mark.parametrize("operation_id, op", operations(), ids=lambda v: str(v)[:30])
def test_every_operation_has_a_matching_method(operation_id, op):
    method = getattr(MailClient, snake(operation_id), None)
    assert method, f"no MailClient.{snake(operation_id)} for {operation_id}"
    params = dict(inspect.signature(method).parameters)
    del params["self"]
    has_user = any(deref(p)["name"] == "user" for p in op.get("parameters", []))
    assert ("user" in params) == has_user
    params.pop("user", None)
    expected = expected_parameters(op)
    assert set(params) == set(expected)
    for name, required in expected.items():
        has_default = params[name].default is not inspect.Parameter.empty
        assert has_default != required, f"{name}: required={required}"


def test_every_method_has_an_operation():
    ids = {snake(operation_id) for operation_id, _ in operations()}
    methods = {
        name
        for name, _ in inspect.getmembers(MailClient, inspect.isfunction)
        if not name.startswith("_") and name not in CONVENIENCE
    }
    assert methods <= ids, f"no operation for {methods - ids}"


def response_schemas() -> set[str]:
    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node and node["$ref"].startswith("#/components/schemas/"):
                name = node["$ref"].rsplit("/", 1)[-1]
                if name not in found:
                    found.add(name)
                    walk(SCHEMAS[name])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for _, op in operations():
        for status, response in op["responses"].items():
            if status.startswith("2"):
                walk(deref(response))
    return found - PAGES


def properties(schema: dict) -> dict[str, bool]:
    """property -> required, with allOf merged."""
    result: dict[str, bool] = {}
    for part in schema.get("allOf", [schema]):
        part = deref(part)
        if "allOf" in part:
            result |= properties(part)
            continue
        required = set(part.get("required", []))
        result |= {snake(k): k in required for k in part.get("properties", {})}
    return result


@pytest.mark.parametrize("name", sorted(response_schemas()))
def test_every_response_schema_has_a_matching_model(name):
    model = getattr(models, name, None)
    assert model, f"no models.{name}"
    expected = properties(SCHEMAS[name])
    actual = {f.name: f for f in fields(model)}
    assert set(actual) == set(expected)
    for field_name, required in expected.items():
        has_default = actual[field_name].default is not MISSING
        assert has_default != required, f"{name}.{field_name}: required={required}"

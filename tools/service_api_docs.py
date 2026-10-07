"""Write the mail client service API's documentation into docs/.

Run:   uv run python tools/service_api_docs.py [URL]

Downloads the OpenAPI document (default: the public one at
https://ttt.stephanides.at/api/documentation/openapi.json, no credentials
needed) and writes two files:

- docs/service-api.openapi.json: the document itself, the machine-readable
  source of truth (formatted, no timestamp);
- docs/service-api.md: a readable reference generated from it.

Re-run it whenever the service changes; `git diff docs/` then shows only real
API changes. Uses only the standard library.
"""

import json
import sys
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://ttt.stephanides.at/api/documentation/openapi.json"
DOCS = Path(__file__).resolve().parent.parent / "docs"
METHODS = ("get", "post", "put", "patch", "delete")


def fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def cell(text) -> str:
    return " ".join(str(text or "").split()).replace("|", "\\|")


def deref(spec: dict, node: dict) -> dict:
    """Follow a local $ref ("#/components/...") to the object it names."""
    while "$ref" in node:
        target = spec
        for part in node["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        node = target
    return node


def type_of(schema: dict) -> str:
    """Short human-readable type for a JSON-schema fragment."""
    if "$ref" in schema:
        return f"[{schema['$ref'].rsplit('/', 1)[-1]}](#{schema['$ref'].rsplit('/', 1)[-1].lower()})"
    if "enum" in schema:
        return " \\| ".join(f"`{v}`" for v in schema["enum"])
    for key in ("anyOf", "oneOf"):
        if key in schema:
            return " \\| ".join(type_of(s) for s in schema[key])
    kind = schema.get("type", "any")
    if isinstance(kind, list):
        kind = " \\| ".join(kind)
    elif kind == "array":
        kind = f"list[{type_of(schema.get('items', {}))}]"
    if schema.get("format"):
        kind += f" ({schema['format']})"
    return kind


def notes(schema: dict) -> str:
    parts = [cell(schema.get("description"))]
    if "default" in schema:
        parts.append(f"Default: `{json.dumps(schema['default'])}`.")
    for key in ("minimum", "maximum", "minLength", "maxLength"):
        if key in schema:
            parts.append(f"{key}: {schema[key]}.")
    return " ".join(p for p in parts if p)


def properties_table(spec: dict, schema: dict) -> list[str]:
    schema = deref(spec, schema)
    if schema.get("type") == "array":
        return [f"A {type_of(schema)}.", ""]
    properties = schema.get("properties")
    if not properties:
        return [f"{type_of(schema)}.", ""]
    required = set(schema.get("required", []))
    lines = ["| Field | Type | Required | Description |", "|---|---|---|---|"]
    for name, prop in properties.items():
        lines.append(
            f"| `{name}` | {type_of(prop)} | {'yes' if name in required else 'no'}"
            f" | {notes(prop)} |"
        )
    return lines + [""]


def scopes(operation: dict) -> str:
    found = [s for req in operation.get("security", []) for s in req.get("oauth2", [])]
    return ", ".join(f"`{s}`" for s in dict.fromkeys(found))


def operation_section(spec: dict, path: str, method: str, operation: dict) -> list[str]:
    lines = [f"### {method.upper()} `{path}`", ""]
    lines += [f"{operation.get('summary', '')} (`{operation['operationId']}`)", ""]
    if operation.get("description"):
        lines += [operation["description"].strip(), ""]
    if scopes(operation):
        lines += [f"Scopes: {scopes(operation)}", ""]

    parameters = [deref(spec, p) for p in operation.get("parameters", [])]
    if parameters:
        lines += [
            "| Parameter | In | Type | Required | Description |",
            "|---|---|---|---|---|",
        ]
        for p in parameters:
            schema = p.get("schema", {})
            lines.append(
                f"| `{p['name']}` | {p['in']} | {type_of(schema)} |"
                f" {'yes' if p.get('required') else 'no'} |"
                f" {notes({**schema, 'description': p.get('description')})} |"
            )
        lines.append("")

    body = operation.get("requestBody")
    if body:
        for media, content in body["content"].items():
            lines += [f"Request body (`{media}`):", ""]
            lines += properties_table(spec, content["schema"])

    lines += ["Responses:", ""]
    for status, response in operation["responses"].items():
        response = deref(spec, response)
        content = response.get("content", {})
        schema = next((c["schema"] for c in content.values() if "schema" in c), None)
        shape = f" {type_of(schema)}" if schema else ""
        lines.append(f"- `{status}`{shape}: {cell(response.get('description'))}")
    return lines + [""]


def render(spec: dict) -> str:
    info = spec["info"]
    lines = [
        f"# {info['title']}",
        "",
        (
            "Generated by `tools/service_api_docs.py` from "
            "[service-api.openapi.json](./service-api.openapi.json): never edit by hand."
        ),
        "",
        f"Version {info['version']}, OpenAPI {spec['openapi']}.",
        "",
        info.get("description", "").strip(),
        "",
        "## Authentication",
        "",
    ]
    for name, scheme in spec["components"]["securitySchemes"].items():
        lines += [f"- `{name}` ({scheme['type']}): {cell(scheme.get('description'))}"]
        for flow in scheme.get("flows", {}).values():
            lines += [f"  - token URL: `{flow['tokenUrl']}`"]
            lines += [f"  - scope `{n}`: {cell(d)}" for n, d in flow["scopes"].items()]
    lines.append("")

    for tag in spec.get("tags", []):
        lines += [f"## {tag['name']}", "", tag.get("description", ""), ""]
        for path, item in spec["paths"].items():
            for method in METHODS:
                operation = item.get(method)
                if operation and tag["name"] in operation.get("tags", []):
                    lines += operation_section(spec, path, method, operation)

    lines += ["## Schemas", ""]
    for name, schema in spec["components"]["schemas"].items():
        lines += [f"### {name}", ""]
        if schema.get("description"):
            lines += [schema["description"].strip(), ""]
        lines += properties_table(spec, schema)
    return "\n".join(lines).replace("\n## Operations\n\n## ", "\n## ").rstrip() + "\n"


def main() -> None:
    spec = fetch(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL)
    DOCS.mkdir(exist_ok=True)
    (DOCS / "service-api.openapi.json").write_text(
        json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (DOCS / "service-api.md").write_text(render(spec), encoding="utf-8")
    print(f"wrote {len(spec['paths'])} paths to docs/service-api.*")


if __name__ == "__main__":
    main()

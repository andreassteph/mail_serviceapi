"""The service API's response shapes as frozen dataclasses.

One class per schema in docs/service-api.openapi.json, same names. Fields are
the schema's properties in snake_case (`hasAttachments` -> `has_attachments`);
date-time fields are timezone-aware `datetime`s; optional properties default to
`None`. `from_json` ignores fields the class does not know, so a newer server
does not break an older client.
"""

import re
import types
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from typing import Any, Self, Union, get_args, get_origin, get_type_hints


def snake(name: str) -> str:
    """camelCase API name -> snake_case Python name."""
    return re.sub(r"(?<!^)([A-Z])", r"_\1", name).lower()


def _convert(hint: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(hint)
    if origin in (Union, types.UnionType):
        hint = next(a for a in get_args(hint) if a is not type(None))
        return _convert(hint, value)
    if origin is list:
        (item,) = get_args(hint)
        return [_convert(item, v) for v in value]
    if hint is datetime:
        return datetime.fromisoformat(value)
    if is_dataclass(hint):
        return hint.from_json(value)
    return value


class Model:
    @classmethod
    def from_json(cls, data: dict) -> Self:
        hints = get_type_hints(cls)
        known = {f.name for f in fields(cls)}
        values = {snake(k): v for k, v in data.items() if snake(k) in known}
        return cls(**{k: _convert(hints[k], v) for k, v in values.items()})


@dataclass(frozen=True, kw_only=True)
class User(Model):
    id: str
    name: str
    email: str


@dataclass(frozen=True, kw_only=True)
class Address(Model):
    name: str
    email: str


@dataclass(frozen=True, kw_only=True)
class Image(Model):
    id: str | None = None
    kind: str | None = None
    source: str | None = None
    alt: str | None = None
    status: str | None = None
    mime_type: str | None = None
    size: int | None = None
    width: int | None = None
    height: int | None = None
    url: str | None = None


@dataclass(frozen=True, kw_only=True)
class AttachmentSummary(Model):
    id: str
    filename: str
    size: int
    mime_type: str
    inline: bool


@dataclass(frozen=True, kw_only=True)
class Attachment(Model):
    id: str
    filename: str
    mime_type: str
    size: int
    inline: bool
    conversion_status: str
    has_text: bool


@dataclass(frozen=True, kw_only=True)
class AttachmentText(Model):
    markdown: str
    truncated: bool
    needs_ocr: bool


@dataclass(frozen=True, kw_only=True)
class RawMessage(Model):
    status: str | None = None
    url: str | None = None
    size: int | None = None
    fetched_at: datetime | None = None
    body_html: str | None = None
    body_text: str | None = None
    truncated: bool | None = None


@dataclass(frozen=True, kw_only=True)
class Mail(Model):
    key: str
    url: str
    sender: str
    email: str
    subject: str
    preview: str
    date: datetime
    unread: bool
    archived: bool
    account: str
    folder: str
    categories: list[str]
    has_attachments: bool
    from_me: bool
    categorized_by: str | None
    thread_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class MailDetail(Mail):
    body_clean_markdown: str
    body_clean_version: int
    images: list[Image]
    to: list[Address]
    cc: list[Address]
    attachments: list[AttachmentSummary]
    body_markdown: str | None = None  # only with include_full_markdown
    raw: RawMessage | None = None


@dataclass(frozen=True, kw_only=True)
class Categorized(Mail):
    dropped_categories: list[str]
    moved_to: str | None = None
    move_conflict: list[str] | None = None


@dataclass(frozen=True, kw_only=True)
class Change(Model):
    at: datetime
    channel: str
    source: str
    field: str
    old: Any
    new: Any
    requested: Any


@dataclass(frozen=True, kw_only=True)
class Category(Model):
    name: str
    color: str
    description: str | None
    parent: str | None
    mail_count: int
    unread_count: int


@dataclass(frozen=True, kw_only=True)
class Draft(Model):
    draft_key: str
    mail: dict | None
    url: str

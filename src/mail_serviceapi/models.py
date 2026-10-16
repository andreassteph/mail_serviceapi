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
class AttachmentWithText(Model):
    id: str
    filename: str
    mime_type: str
    size: int
    inline: bool
    conversion_status: str | None
    markdown: str | None = None  # only when there is text and it was asked for
    truncated: bool | None = None


@dataclass(frozen=True, kw_only=True)
class SuggestedCategory(Model):
    """A pending category suggestion; `note` is untrusted text."""

    category: str
    note: str | None
    confidence: int
    source: str
    status: str


@dataclass(frozen=True, kw_only=True)
class Mail(Model):
    key: str
    url: str
    sender: str
    email: str
    subject: str
    preview: str
    date: datetime
    created_at: datetime  # first stored here, not when it was sent
    changed_at: datetime
    unread: bool
    archived: bool
    account: str
    folder: str
    folders: list[str]
    categories: list[str]
    has_attachments: bool
    from_me: bool
    categorized_by: str | None
    previous_categorized_by: str | None
    thread_id: str | None = None
    suggested_categories: list[SuggestedCategory] | None = None
    contacts: list[dict] | None = None  # only with the scope contacts:read


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
class MailWithContent(Mail):
    """A mail of `list_mails(include_content=True)`: the content of `get_mail`."""

    to: list[Address]
    cc: list[Address]
    message_id: str | None
    body_clean_markdown: str
    body_clean_version: int
    body_truncated: bool
    images: list[Image]
    attachments: list[AttachmentWithText]
    body_markdown: str | None = None  # only with include_full_markdown


@dataclass(frozen=True, kw_only=True)
class Categorized(Mail):
    dropped_categories: list[str]
    moved_to: str | None = None
    move_conflict: list[str] | None = None


@dataclass(frozen=True, kw_only=True)
class BatchError(Model):
    code: str
    message: str


@dataclass(frozen=True, kw_only=True)
class BatchResult(Model):
    """One mail of `batch_categorize_mails`: `ok`, or `error` says why not."""

    key: str
    ok: bool
    categories: list[str] | None = None
    categorized_by: str | None = None
    dropped_categories: list[str] | None = None
    changed: bool | None = None
    moved_to: str | None = None
    move_conflict: list[str] | None = None
    error: BatchError | None = None


@dataclass(frozen=True, kw_only=True)
class BatchCategorized(Model):
    results: list[BatchResult]
    succeeded: int
    failed: int


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
    sort_rules: str | None = None  # Markdown; None = none (or left out)


@dataclass(frozen=True, kw_only=True)
class Draft(Model):
    draft_key: str
    mail: dict | None
    url: str


@dataclass(frozen=True, kw_only=True)
class CategorySuggestionResult(Model):
    key: str
    category: str
    result: str  # created, updated or rejected
    status: str  # pending or rejected
    source: str
    note: str | None
    confidence: int
    message: str


@dataclass(frozen=True, kw_only=True)
class CategorySuggestionDecision(Model):
    """Answer of `decide_category_suggestion`: the mail as it is now."""

    key: str
    category: str
    decision: str  # accepted or rejected
    mail: Mail


@dataclass(frozen=True, kw_only=True)
class Correction(Model):
    """Where `to_source` changed what `from_source` decided about a mail.
    `mail` is `{key}` plus subject, from, ... only with `include_mail`; its text
    is untrusted."""

    at: datetime
    channel: str
    client_id: str | None
    mail: dict
    from_source: str
    to_source: str
    kind: str  # replaced, added, removed or rejected_suggestion
    before: list[str]
    after: list[str]
    added: list[str]
    removed: list[str]
    kept: list[str]
    note: str | None


@dataclass(frozen=True, kw_only=True)
class CorrectionSummary(Model):
    period: dict
    total: int
    groups: list[dict]
    confirmed: list[dict]
    categorizations: list[dict]


@dataclass(frozen=True, kw_only=True)
class SourceName(Model):
    name: str
    reserved: bool
    first_seen: datetime | None
    last_seen: datetime | None
    categorizations: int
    current_mails: int
    suggestions: int


# -- contacts ------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class ContactRef(Model):
    id: str | None = None
    name: str | None = None


@dataclass(frozen=True, kw_only=True)
class ContactAddress(Model):
    address: str
    label: str | None = None
    received_count: int | None = None
    sent_count: int | None = None
    last_seen_at: datetime | None = None


@dataclass(frozen=True, kw_only=True)
class Phone(Model):
    number: str
    label: str | None = None


@dataclass(frozen=True, kw_only=True)
class Url(Model):
    url: str
    label: str | None = None


@dataclass(frozen=True, kw_only=True)
class PostalAddress(Model):
    label: str | None = None
    street: str | None = None
    postal_code: str | None = None
    city: str | None = None
    region: str | None = None
    country: str | None = None


@dataclass(frozen=True, kw_only=True)
class Contact(Model):
    id: str | None = None
    kind: str | None = None  # person or company
    name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    middle_names: str | None = None
    name_prefix: str | None = None
    name_suffix: str | None = None
    nickname: str | None = None
    company: ContactRef | None = None
    job_title: str | None = None
    birthday: str | None = None  # YYYY-MM-DD
    notes: str | None = None
    domains: list[str] | None = None
    link_all_domain_addresses: bool | None = None
    addresses: list[ContactAddress] | None = None
    phones: list[Phone] | None = None
    urls: list[Url] | None = None
    postal_addresses: list[PostalAddress] | None = None
    has_avatar: bool | None = None
    created_by: str | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, kw_only=True)
class ContactWithAvatarSize(Contact):
    avatar_size: dict | None = None  # {width, height}


@dataclass(frozen=True, kw_only=True)
class ImportItem(Model):
    name: str | None = None
    action: str | None = None  # created, updated or skipped
    reason: str | None = None
    contact_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class ImportReport(Model):
    dry_run: bool | None = None
    created: int | None = None
    updated: int | None = None
    skipped: int | None = None
    items: list[ImportItem] | None = None


@dataclass(frozen=True, kw_only=True)
class Suggestion(Model):
    """A contact suggestion for an address (not a category suggestion)."""

    id: str | None = None
    address: str | None = None
    kind: str | None = None  # join or new
    reason: str | None = None
    score: int | None = None
    source: str | None = None
    note: str | None = None
    contact_id: str | None = None
    proposed: dict | None = None  # for kind "new"
    status: str | None = None  # pending, accepted or rejected
    created_at: datetime | None = None


@dataclass(frozen=True, kw_only=True)
class PoolAddress(Model):
    address: str | None = None
    name: str | None = None
    contact: ContactRef | None = None
    label: str | None = None
    received_count: int | None = None
    sent_count: int | None = None
    copied_count: int | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    ignored: bool | None = None
    suggestion: dict | None = None  # the best pending suggestion


@dataclass(frozen=True, kw_only=True)
class SuggestionDecision(Model):
    """Answer of `decide_suggestion`; `contact` is None after a reject."""

    address: PoolAddress
    contact: Contact | None = None

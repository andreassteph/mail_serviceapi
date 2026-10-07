"""Thin Python client for the mail client's REST service API.

One method per operation in docs/service-api.openapi.json, named after its
operationId in snake_case, with the operation's parameters in snake_case.
Differences from the raw API:

- answers are parsed into the dataclasses in `mail_serviceapi.models`;
- `list_mails` and `list_mail_changes` hide the cursor paging: they yield one
  item at a time and fetch the next page only when the current one is used up;
- `user` (the `{user}` in every path) defaults to the client's user;
- a non-2xx answer raises `MailClientError`; a 429 is retried after
  `Retry-After` seconds, up to `max_retries` times.
"""

import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime
from typing import Any, Self

import httpx

from mail_serviceapi.models import (
    Attachment,
    AttachmentText,
    Categorized,
    Category,
    Change,
    Draft,
    Mail,
    MailDetail,
    User,
)

__all__ = ["MailClient", "MailClientError"]

UNSET: Any = object()  # "leave unchanged", distinct from None ("set to null")


class MailClientError(Exception):
    """A non-2xx answer. `code` is the service's stable error code."""

    def __init__(
        self,
        status: int,
        message: str,
        code: str | None = None,
        allowed: Any = None,
        example: Any = None,
    ):
        super().__init__(
            f"{status} {code}: {message}" if code else f"{status}: {message}"
        )
        self.status = status
        self.code = code
        self.message = message
        self.allowed = allowed
        self.example = example


def _drop_none(values: dict) -> dict:
    return {k: v for k, v in values.items() if v is not None}


def _flag(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _drop_unset(values: dict) -> dict:
    return {k: v for k, v in values.items() if v is not UNSET}


def _time(value: str | datetime | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


class MailClient:
    def __init__(
        self,
        url: str,
        token: str,
        user: int | str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
    ):
        """`url` is the service base URL (e.g. https://host), `token` a personal
        token or a service client's access token.

        `user` is the default `{user}` for every call. Left out, the client uses
        the credential's only user (from `list_users`) and raises if it has
        several. `transport` replaces the network, for tests.
        """
        self.user = user
        self.max_retries = max_retries
        self._http = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- plumbing --------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            response = self._http.request(method, path, **kwargs)
            if response.status_code != 429 or attempt == self.max_retries:
                break
            time.sleep(int(response.headers.get("Retry-After", "1")))
        if response.is_success:
            return response
        try:
            body = response.json()
        except ValueError:
            raise MailClientError(response.status_code, response.text) from None
        error = body.get("error") or {}
        raise MailClientError(
            response.status_code,
            error.get("message") or body.get("message", ""),
            code=error.get("code"),
            allowed=error.get("allowed"),
            example=error.get("example"),
        )

    def _base(self, user: int | str | None) -> str:
        if user is None:
            if self.user is None:
                users = self.list_users()
                if len(users) != 1:
                    raise MailClientError(
                        0,
                        f"this credential has {len(users)} users; pass user=",
                        code="ambiguous_user",
                    )
                self.user = users[0].id
            user = self.user
        return f"/api/v1/service/users/{user}"

    def _pages(self, path: str, params: dict) -> Iterator[dict]:
        cursor = None
        while True:
            page = self._request(
                "GET", path, params=_drop_none({**params, "cursor": cursor})
            ).json()
            yield from page["data"]
            cursor = page.get("nextCursor")
            if not cursor:
                return

    # -- users -----------------------------------------------------------------

    def list_users(self) -> list[User]:
        """The users this credential may access; their `id` is the `user`."""
        response = self._request("GET", "/api/v1/service/users")
        return [User.from_json(u) for u in response.json()]

    # -- categories ------------------------------------------------------------

    def list_categories(self, *, user: int | str | None = None) -> list[Category]:
        """The category tree as a flat list, parents before children."""
        response = self._request("GET", f"{self._base(user)}/categories")
        return [Category.from_json(c) for c in response.json()]

    def create_category(
        self,
        name: str,
        *,
        parent: str | None = None,
        description: str | None = None,
        color: str | None = None,
        user: int | str | None = None,
    ) -> Category:
        """Add a category (`name` is its own name, `parent` a full path)."""
        body = _drop_none(
            {"name": name, "parent": parent, "description": description, "color": color}
        )
        response = self._request("POST", f"{self._base(user)}/categories", json=body)
        return Category.from_json(response.json())

    def update_category(
        self,
        category: str,
        *,
        name: str = UNSET,
        description: str | None = UNSET,
        color: str = UNSET,
        parent: str | None = UNSET,
        user: int | str | None = None,
    ) -> Category:
        """Change only the fields given. `category` is the current full path;
        `parent=None` makes it a root, `description=None` clears it."""
        body = _drop_unset(
            {
                "category": category,
                "name": name,
                "description": description,
                "color": color,
                "parent": parent,
            }
        )
        response = self._request("PATCH", f"{self._base(user)}/categories", json=body)
        return Category.from_json(response.json())

    # -- mails -----------------------------------------------------------------

    def list_mails(
        self,
        *,
        view: str | None = None,
        category: str | None = None,
        include_children: bool | None = None,
        account: str | None = None,
        folder: str | None = None,
        unread: bool | None = None,
        archived: bool | None = None,
        categorized_by: str | None = None,
        since: str | datetime | None = None,
        until: str | datetime | None = None,
        q: str | None = None,
        limit: int | None = None,
        user: int | str | None = None,
    ) -> Iterator[Mail]:
        """Every matching mail, newest first, one at a time.

        Pages are fetched lazily: the first when iteration starts, the next only
        when the current one is used up, so only one page is ever held and
        leaving the loop early stops further requests. `limit` is the page size
        (1 to 200). `since`/`until` take a datetime, an ISO time or a duration
        such as "4d".
        """
        params = {
            "view": view,
            "category": category,
            "includeChildren": include_children,
            "account": account,
            "folder": folder,
            "unread": unread,
            "archived": archived,
            "categorizedBy": categorized_by,
            "since": _time(since),
            "until": _time(until),
            "q": q,
            "limit": limit,
        }
        for mail in self._pages(f"{self._base(user)}/mails", params):
            yield Mail.from_json(mail)

    def list_full_mails(
        self,
        *,
        view: str | None = None,
        category: str | None = None,
        include_children: bool | None = None,
        account: str | None = None,
        folder: str | None = None,
        unread: bool | None = None,
        archived: bool | None = None,
        categorized_by: str | None = None,
        since: str | datetime | None = None,
        until: str | datetime | None = None,
        q: str | None = None,
        limit: int | None = None,
        user: int | str | None = None,
    ) -> Iterator[MailDetail]:
        """`list_mails`, but each mail in full via `get_mail`.

        Not an API operation: one extra request per mail, made lazily as the
        loop reaches it.
        """
        for mail in self.list_mails(
            view=view,
            category=category,
            include_children=include_children,
            account=account,
            folder=folder,
            unread=unread,
            archived=archived,
            categorized_by=categorized_by,
            since=since,
            until=until,
            q=q,
            limit=limit,
            user=user,
        ):
            detail = self.get_mail(mail.key, user=user)
            yield replace(detail, **vars(mail))

    def get_mail(
        self,
        key: str,
        *,
        include_full_markdown: bool | None = None,
        include_raw: bool | None = None,
        max_body_chars: int | None = None,
        user: int | str | None = None,
    ) -> MailDetail:
        """One mail in full: the clean body (`body_clean_markdown`, the input for
        sorting), recipients, images, attachments. `include_full_markdown` adds
        `body_markdown`, the body as sent, for showing the mail to a person."""
        params = _drop_none(
            {
                # the service rejects "true" here; it accepts 1/0
                "includeFullMarkdown": _flag(include_full_markdown),
                "includeRaw": _flag(include_raw),
                "maxBodyChars": max_body_chars,
            }
        )
        response = self._request(
            "GET", f"{self._base(user)}/mails/{key}", params=params
        )
        return MailDetail.from_json(response.json())

    def list_mail_changes(
        self, key: str, *, limit: int | None = None, user: int | str | None = None
    ) -> Iterator[Change]:
        """The mail's change log, one entry at a time, paged lazily like
        `list_mails`."""
        path = f"{self._base(user)}/mails/{key}/changes"
        for change in self._pages(path, {"limit": limit}):
            yield Change.from_json(change)

    def categorize_mail(
        self,
        key: str,
        categories: list[str],
        *,
        categorized_by: str | None = None,
        user: int | str | None = None,
    ) -> Categorized:
        """Replace the mail's categories (full paths; [] for none).
        `categorized_by` records who decided."""
        body = _drop_none({"categories": categories, "categorizedBy": categorized_by})
        response = self._request(
            "PUT", f"{self._base(user)}/mails/{key}/categories", json=body
        )
        return Categorized.from_json(response.json())

    # -- drafts ----------------------------------------------------------------

    def create_draft(
        self,
        body: str,
        *,
        reply_to: str | None = None,
        account: str | None = None,
        to: str | None = None,
        subject: str | None = None,
        category: str | None = None,
        replace: bool | None = None,
        user: int | str | None = None,
    ) -> Draft:
        """Create a draft for the user to review and send; never sends mail."""
        payload = _drop_none(
            {
                "body": body,
                "replyTo": reply_to,
                "account": account,
                "to": to,
                "subject": subject,
                "category": category,
                "replace": replace,
            }
        )
        response = self._request("POST", f"{self._base(user)}/drafts", json=payload)
        return Draft.from_json(response.json())

    # -- attachments, source, images -------------------------------------------

    def list_attachments(
        self, key: str, *, user: int | str | None = None
    ) -> list[Attachment]:
        """The mail's attachments and whether their text is available."""
        response = self._request("GET", f"{self._base(user)}/mails/{key}/attachments")
        return [Attachment.from_json(a) for a in response.json()]

    def download_attachment(
        self, key: str, att_id: str, *, user: int | str | None = None
    ) -> bytes:
        """The attachment file itself."""
        path = f"{self._base(user)}/mails/{key}/attachments/{att_id}"
        return self._request("GET", path).content

    def get_attachment_text(
        self, key: str, att_id: str, *, user: int | str | None = None
    ) -> AttachmentText:
        """The attachment converted to markdown."""
        path = f"{self._base(user)}/mails/{key}/attachments/{att_id}/text"
        return AttachmentText.from_json(self._request("GET", path).json())

    def download_mail_source(self, key: str, *, user: int | str | None = None) -> bytes:
        """The original message (RFC 822)."""
        return self._request("GET", f"{self._base(user)}/mails/{key}/source").content

    def get_mail_image(
        self, key: str, image_id: str, *, user: int | str | None = None
    ) -> bytes:
        """One image of the mail's body."""
        path = f"{self._base(user)}/mails/{key}/images/{image_id}"
        return self._request("GET", path).content

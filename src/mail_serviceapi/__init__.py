"""Thin Python client for the mail client's REST service API.

One method per operation in docs/service-api.openapi.json, named after its
operationId in snake_case, with the operation's parameters in snake_case.
Differences from the raw API:

- answers are parsed into the dataclasses in `mail_serviceapi.models`;
- `list_mails` and `list_mail_changes` hide the cursor paging: they yield one
  item at a time and fetch the next page only when the current one is used up;
- `user` (the `{user}` in every path) defaults to the client's user;
- `ClientCredentials` as the token handles requestToken (/oauth/token): it
  fetches a service client's access token and renews it before it expires;
- a non-2xx answer raises `MailClientError`; a 429 is retried after
  `Retry-After` seconds, up to `max_retries` times;
- a dropped connection (e.g. a stale keep-alive one) is retried up to
  `max_retries` times for idempotent requests (GET, PUT, DELETE) only; POST
  and PATCH raise at once, as a repeat could apply their effect twice.
"""

import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime
from urllib.parse import quote
from typing import Any, Self

import httpx

from mail_serviceapi.models import (
    Attachment,
    AttachmentText,
    BatchCategorized,
    Categorized,
    Category,
    CategorySuggestionDecision,
    CategorySuggestionResult,
    Change,
    Contact,
    ContactWithAvatarSize,
    Correction,
    CorrectionSummary,
    Draft,
    ImportReport,
    Mail,
    MailDetail,
    MailWithContent,
    PoolAddress,
    SourceName,
    Suggestion,
    SuggestionDecision,
    User,
)

__all__ = ["ClientCredentials", "MailClient", "MailClientError"]

UNSET: Any = object()  # "leave unchanged", distinct from None ("set to null")

# methods repeated after a dropped connection; POST and PATCH are not
_IDEMPOTENT = frozenset({"GET", "PUT", "DELETE"})


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


class ClientCredentials(httpx.Auth):
    """A service client's access tokens, fetched from /oauth/token with the
    client credentials grant and fetched anew shortly before they expire (they
    live 10 minutes) or when an answer is 401. Pass it as `token`; one instance
    may be shared by several clients."""

    requires_response_body = True
    EARLY = 30  # seconds before expiry a token is replaced

    def __init__(
        self, url: str, client_id: str, client_secret: str, scope: str | None = None
    ):
        self.token_url = url.rstrip("/") + "/oauth/token"
        self.form = _drop_none(
            {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": scope,
            }
        )
        self.access_token: str | None = None
        self.expires_at = 0.0  # time.monotonic()

    def auth_flow(self, request: httpx.Request):
        if self.access_token is None or time.monotonic() >= self.expires_at:
            self._store((yield httpx.Request("POST", self.token_url, data=self.form)))
        request.headers["Authorization"] = f"Bearer {self.access_token}"
        response = yield request
        if response.status_code == 401:  # revoked or expired early: once more
            self._store((yield httpx.Request("POST", self.token_url, data=self.form)))
            request.headers["Authorization"] = f"Bearer {self.access_token}"
            yield request

    def _store(self, response: httpx.Response) -> None:
        if not response.is_success:
            try:  # OAuth error body: error, error_description
                body = response.json()
                code, message = body.get("error"), body.get("error_description", "")
            except ValueError:
                code, message = None, response.text
            raise MailClientError(response.status_code, message, code=code)
        body = response.json()
        self.access_token = body["access_token"]
        self.expires_at = time.monotonic() + body["expires_in"] - self.EARLY


def _drop_none(values: dict) -> dict:
    return {k: v for k, v in values.items() if v is not None}


def _flag(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _drop_unset(values: dict) -> dict:
    return {k: v for k, v in values.items() if v is not UNSET}


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(word.capitalize() for word in rest)


def _body(values: dict) -> dict:
    """A request body from a method's arguments: camelCase keys, UNSET left out
    (None is kept: it is JSON null)."""
    return {_camel(k): v for k, v in values.items() if v is not UNSET}


def _query(values: dict) -> dict:
    """Query parameters: camelCase keys, None left out, bools as 1/0, datetimes
    as ISO text."""
    return {
        _camel(k.rstrip("_")): _flag(v) if isinstance(v, bool) else _time(v)
        for k, v in values.items()
        if v is not None
    }


def _time(value: str | datetime | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


class MailClient:
    def __init__(
        self,
        url: str,
        token: str | ClientCredentials,
        user: int | str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
    ):
        """`url` is the service base URL (e.g. https://host), `token` a personal
        token, a service client's access token or `ClientCredentials`, which
        fetches and renews the access tokens itself.

        `user` is the default `{user}` for every call. Left out, the client uses
        the credential's only user (from `list_users`) and raises if it has
        several. `transport` replaces the network, for tests.
        """
        self.user = user
        self.max_retries = max_retries
        headers = {"Accept": "application/json"}
        if isinstance(token, str):
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(
            base_url=url.rstrip("/"),
            headers=headers,
            auth=None if isinstance(token, str) else token,
            timeout=timeout,
            transport=transport,
            # no idle keep-alive: a pooled connection the server closed meanwhile
            # fails a POST with "Server disconnected", which can't be retried safely
            limits=httpx.Limits(max_keepalive_connections=0),
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
            try:
                response = self._http.request(method, path, **kwargs)
            except httpx.TransportError:
                # a pooled keep-alive connection the server already closed;
                # only idempotent requests are safe to repeat
                if method not in _IDEMPOTENT or attempt == self.max_retries:
                    raise
                time.sleep(1)
                continue
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

    def _pages(self, path: str, params: dict, key: str = "data") -> Iterator[dict]:
        cursor = None
        while True:
            page = self._request(
                "GET", path, params=_drop_none({**params, "cursor": cursor})
            ).json()
            yield from page[key]
            cursor = page.get("nextCursor")
            if not cursor:
                return

    # -- users -----------------------------------------------------------------

    def list_users(self) -> list[User]:
        """The users this credential may access; their `id` is the `user`."""
        response = self._request("GET", "/api/v1/service/users")
        return [User.from_json(u) for u in response.json()]

    # -- categories ------------------------------------------------------------

    def list_categories(
        self, *, include_sort_rules: bool | None = None, user: int | str | None = None
    ) -> list[Category]:
        """The category tree as a flat list, parents before children.
        `include_sort_rules=False` leaves each category's `sort_rules` out."""
        response = self._request(
            "GET",
            f"{self._base(user)}/categories",
            params=_query({"include_sort_rules": include_sort_rules}),
        )
        return [Category.from_json(c) for c in response.json()]

    def create_category(
        self,
        name: str,
        *,
        parent: str | None = None,
        description: str | None = None,
        sort_rules: str | None = None,
        color: str | None = None,
        user: int | str | None = None,
    ) -> Category:
        """Add a category (`name` is its own name, `parent` a full path).
        `sort_rules` is Markdown guidance on what belongs here (up to 5000
        characters)."""
        body = _drop_none(
            {
                "name": name,
                "parent": parent,
                "description": description,
                "sortRules": sort_rules,
                "color": color,
            }
        )
        response = self._request("POST", f"{self._base(user)}/categories", json=body)
        return Category.from_json(response.json())

    def update_category(
        self,
        category: str,
        *,
        name: str = UNSET,
        description: str | None = UNSET,
        sort_rules: str | None = UNSET,
        color: str = UNSET,
        parent: str | None = UNSET,
        user: int | str | None = None,
    ) -> Category:
        """Change only the fields given. `category` is the current full path;
        `parent=None` makes it a root, `description=None` clears it,
        `sort_rules` (None or "" clears) replaces the whole Markdown text."""
        body = _drop_unset(
            {
                "category": category,
                "name": name,
                "description": description,
                "sortRules": sort_rules,
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
        contact: str | None = None,
        suggested: bool | None = None,
        previous_categorized_by: str | None = None,
        changed_since: str | datetime | None = None,
        changed_until: str | datetime | None = None,
        created_since: str | datetime | None = None,
        created_until: str | datetime | None = None,
        order_by: str | None = None,
        limit: int | None = None,
        include_content: bool | None = None,
        max_body_chars: int | None = None,
        max_attachment_chars: int | None = None,
        include_attachment_text: bool | None = None,
        include_full_markdown: bool | None = None,
        user: int | str | None = None,
    ) -> Iterator[Mail | MailWithContent]:
        """Every matching mail, newest first, one at a time.

        Pages are fetched lazily: the first when iteration starts, the next only
        when the current one is used up, so only one page is ever held and
        leaving the loop early stops further requests. `limit` is the page size
        (1 to 500; 1 to 100 with `include_content`). `since`/`until` take a
        datetime, an ISO time or a duration such as "4d" and filter by when the mail
        was sent. `created_since`/`created_until` filter by when it was first
        stored here (`created_at`), `changed_since`/`changed_until` by when it
        last changed here (`changed_at`; same forms). `order_by` is "date" (sent,
        the default), "changed" or "created", always newest first; a cursor only
        fits its order. To poll for news: pass the newest `changed_at` you have
        seen as `changed_since` with `order_by="changed"`. `suggested=True` keeps
        mails with a pending category suggestion.

        With `include_content=True` every item is a `MailWithContent`: the
        content of `get_mail` comes with the list, so no `get_mail` per mail is
        needed. `max_body_chars`, `max_attachment_chars`,
        `include_attachment_text` and `include_full_markdown` only work with it.
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
            "contact": contact,
            "suggested": _flag(suggested),
            "previousCategorizedBy": previous_categorized_by,
            "changedSince": _time(changed_since),
            "changedUntil": _time(changed_until),
            "createdSince": _time(created_since),
            "createdUntil": _time(created_until),
            "orderBy": order_by,
            "limit": limit,
            "includeContent": _flag(include_content),
            "maxBodyChars": max_body_chars,
            "maxAttachmentChars": max_attachment_chars,
            "includeAttachmentText": _flag(include_attachment_text),
            "includeFullMarkdown": _flag(include_full_markdown),
        }
        model = MailWithContent if include_content else Mail
        for mail in self._pages(f"{self._base(user)}/mails", params):
            yield model.from_json(mail)

    def list_full_mails(
        self, *, limit: int | None = None, **filters: Any
    ) -> Iterator[MailWithContent]:
        """`list_mails(include_content=True)`: each mail with its content, paged
        in batches of `limit` (at most 100) instead of one request per mail.
        Takes the same arguments as `list_mails`."""
        return self.list_mails(include_content=True, limit=limit, **filters)

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

    def batch_categorize_mails(
        self,
        items: list[dict[str, Any]],
        *,
        categorized_by: str | None = None,
        user: int | str | None = None,
    ) -> BatchCategorized:
        """Set the categories of up to 100 mails in one request, each like
        `categorize_mail`. An item is `{"key": ..., "categories": [...]}` and
        may carry its own `"categorized_by"`. The items are independent: a
        failed one has `ok=False` and an `error` in its result, the others are
        still applied; retry only the failed keys."""
        body = _drop_none(
            {
                "categorizedBy": categorized_by,
                "items": [
                    _drop_none(
                        {
                            "key": item["key"],
                            "categories": item["categories"],
                            "categorizedBy": item.get("categorized_by"),
                        }
                    )
                    for item in items
                ],
            }
        )
        response = self._request(
            "POST", f"{self._base(user)}/mails/categorize", json=body
        )
        return BatchCategorized.from_json(response.json())

    def suggest_category(
        self,
        key: str,
        category: str,
        *,
        note: str | None = UNSET,
        confidence: int = UNSET,
        source: str = UNSET,
        user: int | str | None = None,
    ) -> CategorySuggestionResult:
        """Propose a category for the mail's conversation when you are not sure
        enough to set it. Nothing changes on the mail; the user decides in the
        app. `result` says whether the suggestion was created, updated or (the
        user rejected it before) rejected. 409 `already_assigned` and
        `too_many_suggestions` raise `MailClientError`."""
        body = _body(
            {
                "category": category,
                "note": note,
                "confidence": confidence,
                "source": source,
            }
        )
        response = self._request(
            "POST", f"{self._base(user)}/mails/{key}/category-suggestions", json=body
        )
        return CategorySuggestionResult.from_json(response.json())

    def withdraw_category_suggestion(
        self, key: str, category: str, *, user: int | str | None = None
    ) -> bool:
        """Withdraw the pending suggestion for `category` (a full path); a
        suggestion that is not pending raises 409 `suggestion_not_pending`."""
        path = f"{self._base(user)}/mails/{key}/category-suggestions/" + quote(
            category, safe="/"
        )
        return self._request("DELETE", path).json()["withdrawn"]

    def decide_category_suggestion(
        self,
        key: str,
        category: str,
        action: str,
        *,
        categorized_by: str | None = None,
        user: int | str | None = None,
    ) -> CategorySuggestionDecision:
        """Accept or reject the pending suggestion for the whole conversation
        (`action` is "accept" or "reject"). Needs the scope `mails:decide`: decide
        only what the user asked you to decide."""
        path = f"{self._base(user)}/mails/{key}/category-suggestions/" + quote(
            category, safe="/"
        )
        body = _drop_none({"action": action, "categorizedBy": categorized_by})
        response = self._request("POST", path + "/decision", json=body)
        return CategorySuggestionDecision.from_json(response.json())

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

    # -- corrections -----------------------------------------------------------

    def list_corrections(
        self,
        *,
        from_: str | None = None,
        to: str | None = None,
        category: str | None = None,
        kind: str | None = None,
        since: str | datetime | None = None,
        until: str | datetime | None = None,
        same_source: bool | None = None,
        include_rejected_suggestions: bool | None = None,
        include_mail: bool | None = None,
        limit: int | None = None,
        user: int | str | None = None,
    ) -> Iterator[Correction]:
        """Where one source changed what another had decided about a mail's
        categories, newest first, one at a time (paged lazily). `from_` is the
        API's `from`: the source that decided first ("llm", "llm,vector",
        "any_algorithm"); `to` the one that changed it. `kind` is replaced, added,
        removed or rejected_suggestion."""
        params = _query(
            {
                "from_": from_,
                "to": to,
                "category": category,
                "kind": kind,
                "since": since,
                "until": until,
                "same_source": same_source,
                "include_rejected_suggestions": include_rejected_suggestions,
                "include_mail": include_mail,
                "limit": limit,
            }
        )
        for correction in self._pages(f"{self._base(user)}/corrections", params):
            yield Correction.from_json(correction)

    def summarize_corrections(
        self,
        *,
        from_: str | None = None,
        to: str | None = None,
        category: str | None = None,
        kind: str | None = None,
        since: str | datetime | None = None,
        until: str | datetime | None = None,
        same_source: bool | None = None,
        include_rejected_suggestions: bool | None = None,
        group_by: str | None = None,
        user: int | str | None = None,
    ) -> CorrectionSummary:
        """Counts of corrections (same filters as `list_corrections`), grouped by
        from and to, optionally also by "category" or "day" (`group_by`)."""
        params = _query(
            {
                "from_": from_,
                "to": to,
                "category": category,
                "kind": kind,
                "since": since,
                "until": until,
                "same_source": same_source,
                "include_rejected_suggestions": include_rejected_suggestions,
                "group_by": group_by,
            }
        )
        response = self._request(
            "GET", f"{self._base(user)}/corrections/summary", params=params
        )
        return CorrectionSummary.from_json(response.json())

    def list_sources(self, *, user: int | str | None = None) -> list[SourceName]:
        """Every source name seen (values of `categorized_by` and suggestion
        sources): look here to reuse the exact name."""
        response = self._request("GET", f"{self._base(user)}/sources")
        return [SourceName.from_json(s) for s in response.json()]

    # -- contacts --------------------------------------------------------------

    def list_contacts(
        self,
        *,
        q: str | None = None,
        kind: str | None = None,
        company: str | None = None,
        limit: int | None = None,
        format: str | None = None,
        user: int | str | None = None,
    ) -> Iterator[Contact | list]:
        """The user's contacts, one at a time (paged lazily; `limit` is the page
        size, 1 to 100). `kind` is "person" or "company", `company` a company's id
        or exact name. With `format="jcard"` every item is the raw jCard array."""
        params = _query(
            {"q": q, "kind": kind, "company": company, "limit": limit, "format": format}
        )
        for contact in self._pages(
            f"{self._base(user)}/contacts", params, key="contacts"
        ):
            yield contact if format == "jcard" else Contact.from_json(contact)

    def save_contact(
        self,
        *,
        id: str = UNSET,
        kind: str = UNSET,
        name: str = UNSET,
        first_name: str = UNSET,
        last_name: str = UNSET,
        middle_names: str = UNSET,
        name_prefix: str = UNSET,
        name_suffix: str = UNSET,
        nickname: str = UNSET,
        company: str | None = UNSET,
        job_title: str = UNSET,
        birthday: str | None = UNSET,
        notes: str = UNSET,
        addresses: list[str] = UNSET,
        domains: list[str] = UNSET,
        link_all_domain_addresses: bool = UNSET,
        phones: list[dict] = UNSET,
        urls: list[dict] = UNSET,
        postal_addresses: list[dict] = UNSET,
        user: int | str | None = None,
    ) -> Contact:
        """Create a contact (without `id`; `kind` "person" or "company" is then
        required) or change one (with `id`): only the fields given change, lists
        replace the old list. `company=None` and `birthday=None` remove them.
        `phones` are `{"label", "number"}`, `urls` `{"label", "url"}`,
        `postal_addresses` `{"label", "street", "postalCode", "city", "region",
        "country"}`."""
        body = _body(
            {
                "id": id,
                "kind": kind,
                "name": name,
                "first_name": first_name,
                "last_name": last_name,
                "middle_names": middle_names,
                "name_prefix": name_prefix,
                "name_suffix": name_suffix,
                "nickname": nickname,
                "company": company,
                "job_title": job_title,
                "birthday": birthday,
                "notes": notes,
                "addresses": addresses,
                "domains": domains,
                "link_all_domain_addresses": link_all_domain_addresses,
                "phones": phones,
                "urls": urls,
                "postal_addresses": postal_addresses,
            }
        )
        response = self._request("POST", f"{self._base(user)}/contacts", json=body)
        return Contact.from_json(response.json())

    def get_contact(
        self,
        id: str,
        *,
        format: str | None = None,
        user: int | str | None = None,
    ) -> Contact | list:
        """One contact; with `format="jcard"` the raw jCard array."""
        response = self._request(
            "GET",
            f"{self._base(user)}/contacts/{id}",
            params=_query({"format": format}),
        )
        data = response.json()
        return data if format == "jcard" else Contact.from_json(data)

    def update_contact(
        self,
        id: str,
        *,
        kind: str = UNSET,
        name: str = UNSET,
        first_name: str = UNSET,
        last_name: str = UNSET,
        middle_names: str = UNSET,
        name_prefix: str = UNSET,
        name_suffix: str = UNSET,
        nickname: str = UNSET,
        company: str | None = UNSET,
        job_title: str = UNSET,
        birthday: str | None = UNSET,
        notes: str = UNSET,
        addresses: list[str] = UNSET,
        domains: list[str] = UNSET,
        link_all_domain_addresses: bool = UNSET,
        phones: list[dict] = UNSET,
        urls: list[dict] = UNSET,
        postal_addresses: list[dict] = UNSET,
        user: int | str | None = None,
    ) -> Contact:
        """Change the contact `id`; only the fields given change (see
        `save_contact`)."""
        body = _body(
            {
                "kind": kind,
                "name": name,
                "first_name": first_name,
                "last_name": last_name,
                "middle_names": middle_names,
                "name_prefix": name_prefix,
                "name_suffix": name_suffix,
                "nickname": nickname,
                "company": company,
                "job_title": job_title,
                "birthday": birthday,
                "notes": notes,
                "addresses": addresses,
                "domains": domains,
                "link_all_domain_addresses": link_all_domain_addresses,
                "phones": phones,
                "urls": urls,
                "postal_addresses": postal_addresses,
            }
        )
        response = self._request("PUT", f"{self._base(user)}/contacts/{id}", json=body)
        return Contact.from_json(response.json())

    def delete_contact(self, id: str, *, user: int | str | None = None) -> None:
        """Delete the contact; its addresses stay in the pool without one."""
        self._request("DELETE", f"{self._base(user)}/contacts/{id}")

    def set_contact_avatar(
        self,
        id: str,
        *,
        image: str | None = None,
        url: str | None = None,
        avatar: bytes | None = None,
        user: int | str | None = None,
    ) -> ContactWithAvatarSize:
        """Set the contact's picture: give exactly one of `image` (base64), `url`
        (the server downloads it) or `avatar` (the file's bytes, uploaded as
        multipart). JPEG, PNG, WebP or GIF up to 2 MB."""
        path = f"{self._base(user)}/contacts/{id}/avatar"
        if avatar is not None:
            kwargs: dict[str, Any] = {
                "files": {"avatar": avatar},
                "data": _drop_none({"image": image, "url": url}),
            }
        else:
            kwargs = {"json": _drop_none({"image": image, "url": url})}
        return ContactWithAvatarSize.from_json(
            self._request("PUT", path, **kwargs).json()
        )

    def delete_contact_avatar(self, id: str, *, user: int | str | None = None) -> Contact:
        """Remove the contact's picture (none is fine)."""
        response = self._request("DELETE", f"{self._base(user)}/contacts/{id}/avatar")
        return Contact.from_json(response.json())

    def export_contacts(
        self,
        *,
        ids: str | None = None,
        q: str | None = None,
        user: int | str | None = None,
    ) -> str:
        """A vCard 4.0 file (text) of at most 200 contacts: the comma separated
        `ids`, or all matching `q`."""
        response = self._request(
            "GET",
            f"{self._base(user)}/contacts.vcf",
            params=_query({"ids": ids, "q": q}),
        )
        return response.text

    def import_contacts(
        self,
        vcard: str,
        *,
        dry_run: bool | None = None,
        user: int | str | None = None,
    ) -> ImportReport:
        """Import vCard 3.0/4.0 text: the same UID or address updates, otherwise
        creates. `dry_run` defaults to true on the server: it only reports until
        you pass `dry_run=False`."""
        body = _drop_none({"vcard": vcard, "dryRun": dry_run})
        response = self._request("POST", f"{self._base(user)}/contacts/import", json=body)
        return ImportReport.from_json(response.json())

    def list_addresses(
        self,
        *,
        linked: bool | None = None,
        q: str | None = None,
        domain: str | None = None,
        min_interactions: int | None = None,
        sort: str | None = None,
        limit: int | None = None,
        user: int | str | None = None,
    ) -> Iterator[PoolAddress]:
        """Every address that appeared in the user's mail with its contact and
        mail counts, one at a time (paged lazily). `linked` keeps only linked
        (True) or unlinked (False) addresses; `sort` is "recent" or
        "interactions"."""
        params = _query(
            {
                "linked": linked,
                "q": q,
                "domain": domain,
                "min_interactions": min_interactions,
                "sort": sort,
                "limit": limit,
            }
        )
        for address in self._pages(
            f"{self._base(user)}/addresses", params, key="addresses"
        ):
            yield PoolAddress.from_json(address)

    def update_address(
        self,
        address: str,
        *,
        contact_id: str | None = UNSET,
        label: str | None = UNSET,
        ignored: bool = UNSET,
        user: int | str | None = None,
    ) -> PoolAddress:
        """Link the address to a contact (`contact_id`; None unlinks it), set its
        `label` (None removes it) or mark it `ignored` ("never suggest")."""
        path = f"{self._base(user)}/addresses/" + quote(address, safe="@")
        body = _body({"contact_id": contact_id, "label": label, "ignored": ignored})
        return PoolAddress.from_json(self._request("PUT", path, json=body).json())

    def list_suggestions(
        self,
        *,
        status: str | None = None,
        min_score: int | None = None,
        domain: str | None = None,
        limit: int | None = None,
        user: int | str | None = None,
    ) -> Iterator[Suggestion]:
        """Contact suggestions for addresses without a contact, newest first, one
        at a time (paged lazily). `status` is "pending" (default), "accepted" or
        "rejected"."""
        params = _query(
            {"status": status, "min_score": min_score, "domain": domain, "limit": limit}
        )
        for suggestion in self._pages(
            f"{self._base(user)}/suggestions", params, key="suggestions"
        ):
            yield Suggestion.from_json(suggestion)

    def propose_suggestion(
        self,
        address: str,
        *,
        contact_id: str | None = None,
        proposed: dict | None = None,
        note: str | None = None,
        confidence: int | None = None,
        source: str | None = None,
        user: int | str | None = None,
    ) -> Suggestion:
        """Propose to add an address without a contact to the contact
        `contact_id`, or to create one (`proposed`: {"type", "name", "firstName",
        "lastName", "companyId" or "companyName", "mainDomain"}); give exactly
        one. Stored pending for the user; it never links by itself."""
        body = _drop_none(
            _body(
                {
                    "address": address,
                    "contact_id": contact_id,
                    "proposed": proposed,
                    "note": note,
                    "confidence": confidence,
                    "source": source,
                }
            )
        )
        response = self._request("POST", f"{self._base(user)}/suggestions", json=body)
        return Suggestion.from_json(response.json())

    def decide_suggestion(
        self,
        id: str,
        action: str,
        *,
        overrides: dict | None = None,
        user: int | str | None = None,
    ) -> SuggestionDecision:
        """Accept (link the address, or create the proposed contact; `overrides`
        change its fields) or reject ("never offered again") a contact
        suggestion. `action` is "accept" or "reject"."""
        body = _drop_none({"action": action, "overrides": overrides})
        response = self._request(
            "POST", f"{self._base(user)}/suggestions/{id}/decision", json=body
        )
        return SuggestionDecision.from_json(response.json())

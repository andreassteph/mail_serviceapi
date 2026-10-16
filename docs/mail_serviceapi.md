# Using mail_serviceapi

`mail_serviceapi` is a thin Python client for the mail client's REST service API.
Every operation in the [API reference](./service-api.md) is one method on
`MailClient`, and every answer is a small, read-only dataclass from
`mail_serviceapi.models`.

## Connect

```python
from mail_serviceapi import MailClient

with MailClient("https://mail.stephanides.at", token) as client:
    print(client.list_users())
```

- `url` is the service's base URL (not the `/mcp` endpoint).
- `token` is a personal token (created in the app under Einstellungen,
  API-Tokens) or a service client's access token. The token's scopes limit what
  works: `mails:read`, `mails:categorize`, `categories:read`,
  `categories:write`, `drafts:write`.
- For a service client, pass `ClientCredentials(url, client_id, client_secret,
  scope=...)` as `token`. It fetches an access token from `/oauth/token` on the
  first call and fetches a new one shortly before it expires (they live 10
  minutes), or after a 401. One instance can be shared by several clients, e.g.
  one per user.
- `user` (optional) is the user whose mail every call uses. Leave it out when
  the token belongs to one user: the client looks that user up on the first
  call. Each method also takes `user=` to override it for one call.

`run.py` reads the URL and token from `MAILCLIENT_URL` and `MAILCLIENT_TOKEN`
in `.env` (see `.env.example`).

## Iterate over mail

`list_mails` returns an iterator of `Mail` objects. Paging is hidden. The
client fetches one page when the loop starts and the next page only after the
loop has used up the current one. The complete list is never held in memory,
and leaving the loop early sends no further requests.

```python
for mail in client.list_mails(view="inbox", category="none", since="7d"):
    print(mail.date, mail.sender, mail.subject, mail.categories)
```

The filters are the API's: `view`, `category` (a full path, `"none"` or
`"any"`), `include_children`, `account`, `folder`, `unread`, `archived`,
`categorized_by`, `since` and `until` (a `datetime`, an ISO time or a duration
such as `"4d"`), `q` (full-text search), `contact` and `limit` (the page size, 1 to 500).

With `include_content=True` every mail is a `MailWithContent`: the fields of
`get_mail` (clean body, recipients, images, attachments with text) come with
the list, so no `get_mail` per mail is needed. Pages hold at most 100 mails;
`max_body_chars`, `max_attachment_chars`, `include_attachment_text` and
`include_full_markdown` tune the content.

`list_mail_changes(key)` works the same way for a mail's change log.

If you need a list, use `list(client.list_mails(...))`. That loads everything.

## Read one mail

```python
detail = client.get_mail(mail.key, max_body_chars=5000)
detail.body_clean_markdown, detail.to, detail.attachments
```

`MailDetail` has every `Mail` field plus the clean body, recipients, images and
attachments. `body_clean_markdown` is the body reduced to readable text (no
images, link targets, signatures, quoted replies or footers): use it for
sorting and search. `body_clean_version` is the version of the rules that made
it (0 = fallback from the plain text part).
`get_mail(..., include_full_markdown=True)` adds `body_markdown`, the body as
sent, for showing a mail to a person; it is `None` otherwise. `list_full_mails(...)` is `list_mails(include_content=True, ...)`. `get_attachment_text`, `download_attachment`,
`download_mail_source` and `get_mail_image` fetch the rest. The `download_*`
and `get_mail_image` methods return `bytes`.

## Sort mail

```python
client.categorize_mail(mail.key, ["Rechnungen/Strom"], categorized_by="mailclient_sort")
```

This replaces the mail's categories (`[]` removes them all). Always pass
`categorized_by`, so the mail records who decided. `list_categories`,
`create_category` and `update_category` manage the category tree. In
`update_category`, only the fields you pass change. `parent=None` makes a
category a root, and `description=None` clears its description.

To sort many mails at once, use `batch_categorize_mails` (up to 100 per
request, one rate-limit hit):

```python
result = client.batch_categorize_mails(
    [{"key": m.key, "categories": ["Rechnungen/Strom"]} for m in mails],
    categorized_by="mailclient_sort",
)
failed = [r.key for r in result.results if not r.ok]  # r.error.code says why
```

The items are independent: a failed one does not stop the others, so retry only
the failed keys. An item may carry its own `"categorized_by"`.

`create_draft` saves a draft for you to review and never sends mail.

## Models

Field names are the API's in snake_case: `hasAttachments` becomes
`has_attachments` and `categorizedBy` becomes `categorized_by`. Date-time
fields are timezone-aware `datetime`s. Optional fields are `None` when the
server leaves them out. Fields the client does not know yet are ignored.

## Errors

Any answer other than success raises `MailClientError` with:

- `status`: the HTTP status;
- `code`: the service's stable error code, for example `unknown_key` or
  `unknown_category`;
- `message`;
- `allowed` and `example`, when the service sends them.

A `429` (rate limited) waits for the `Retry-After` seconds and retries, up to
`max_retries` times (default 3).

```python
from mail_serviceapi import MailClientError

try:
    client.get_mail("nope")
except MailClientError as error:
    print(error.status, error.code, error.message)
```

## When the API changes

Run `uv run python tools/service_api_docs.py`, then `uv run pytest`.
`tests/test_spec.py` lists every method, parameter or model field that no
longer matches the API.

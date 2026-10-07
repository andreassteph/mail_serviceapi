"""MailClient behaviour against a fake server (httpx.MockTransport), fake data only."""

import json
from datetime import UTC, datetime

import httpx
import pytest

import mail_serviceapi
from mail_serviceapi import MailClient, MailClientError
from mail_serviceapi.models import Categorized, Mail, MailDetail

BASE = "/api/v1/service/users/3"


def fake_mail(key: str, **extra) -> dict:
    return {
        "key": key,
        "url": f"https://mail.example/m/{key}",
        "sender": "Stadtwerke",
        "email": "rechnung@stadtwerke.example",
        "subject": f"Rechnung {key}",
        "preview": "Ihre Abschlagszahlung",
        "date": "2026-09-01T08:00:00+02:00",
        "unread": True,
        "archived": False,
        "account": "Privat",
        "folder": "Posteingang",
        "categories": [],
        "hasAttachments": False,
        "fromMe": False,
        "categorizedBy": None,
        "someFutureField": "ignored",
        **extra,
    }


class FakeServer:
    """Answers requests from a dict of (method, path) -> answer, records them."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.routes[(request.method, request.url.path)]
        return answer(request) if callable(answer) else answer

    def client(self, **kwargs) -> MailClient:
        kwargs.setdefault("user", 3)
        return MailClient(
            "https://mail.example/",
            "tok",
            transport=httpx.MockTransport(self),
            **kwargs,
        )


def paged(pages: list[list[dict]]):
    """Handler serving `pages` in order, chained by cursors "c1", "c2", ..."""

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        index = int(cursor[1:]) if cursor else 0
        next_cursor = f"c{index + 1}" if index + 1 < len(pages) else None
        page = {
            "data": pages[index],
            "nextCursor": next_cursor,
            "endCursor": None,
            "canLoadOlder": False,
        }
        return httpx.Response(200, json=page)

    return handler


def test_sends_bearer_token_to_base_url():
    server = FakeServer({("GET", f"{BASE}/categories"): httpx.Response(200, json=[])})
    server.client().list_categories()
    request = server.requests[0]
    assert request.headers["Authorization"] == "Bearer tok"
    assert str(request.url) == f"https://mail.example{BASE}/categories"


def test_user_defaults_to_the_only_user_of_the_credential():
    me = [{"id": "3", "name": "A", "email": "a@example.org"}]
    server = FakeServer(
        {
            ("GET", "/api/v1/service/users"): httpx.Response(200, json=me),
            ("GET", f"{BASE}/categories"): httpx.Response(200, json=[]),
        }
    )
    client = server.client(user=None)
    client.list_categories()
    client.list_categories()
    paths = [r.url.path for r in server.requests]
    assert paths == [
        "/api/v1/service/users",
        f"{BASE}/categories",
        f"{BASE}/categories",
    ]


def test_user_default_refuses_several_users():
    users = [{"id": str(i), "name": "A", "email": "a@example.org"} for i in (3, 4)]
    server = FakeServer(
        {("GET", "/api/v1/service/users"): httpx.Response(200, json=users)}
    )
    with pytest.raises(MailClientError, match="2 users"):
        server.client(user=None).list_categories()


def test_explicit_user_overrides_the_default():
    path = "/api/v1/service/users/7/categories"
    server = FakeServer({("GET", path): httpx.Response(200, json=[])})
    server.client().list_categories(user=7)
    assert server.requests[0].url.path == path


def test_list_mails_yields_mail_objects_across_pages():
    pages = [[fake_mail("a"), fake_mail("b")], [fake_mail("c")], [fake_mail("d")]]
    server = FakeServer({("GET", f"{BASE}/mails"): paged(pages)})
    mails = list(server.client().list_mails(view="inbox"))
    assert [m.key for m in mails] == ["a", "b", "c", "d"]
    assert all(type(m) is Mail for m in mails)
    assert mails[0].date == datetime(2026, 9, 1, 6, 0, tzinfo=UTC)
    assert mails[0].has_attachments is False and mails[0].thread_id is None
    assert [r.url.params.get("cursor") for r in server.requests] == [None, "c1", "c2"]


def test_list_mails_fetches_the_next_page_only_when_needed():
    pages = [[fake_mail("a"), fake_mail("b")], [fake_mail("c")]]
    server = FakeServer({("GET", f"{BASE}/mails"): paged(pages)})
    mails = server.client().list_mails()
    assert server.requests == []  # nothing until iteration starts
    assert next(mails).key == "a"
    assert next(mails).key == "b"
    assert len(server.requests) == 1  # second page not yet requested
    assert next(mails).key == "c"
    assert len(server.requests) == 2


def test_list_mails_stops_requesting_after_break():
    pages = [[fake_mail("a"), fake_mail("b")], [fake_mail("c")]]
    server = FakeServer({("GET", f"{BASE}/mails"): paged(pages)})
    for _ in server.client().list_mails():
        break
    assert len(server.requests) == 1


def test_list_mails_empty():
    server = FakeServer({("GET", f"{BASE}/mails"): paged([[]])})
    assert list(server.client().list_mails()) == []
    assert len(server.requests) == 1


def test_list_mails_maps_parameters():
    server = FakeServer({("GET", f"{BASE}/mails"): paged([[]])})
    since = datetime(2026, 9, 1, tzinfo=UTC)
    mails = server.client().list_mails(
        category="none",
        include_children=False,
        categorized_by="llm",
        since=since,
        until="1w",
        q="strom",
        limit=50,
    )
    list(mails)
    assert dict(server.requests[0].url.params) == {
        "category": "none",
        "includeChildren": "false",
        "categorizedBy": "llm",
        "since": "2026-09-01T00:00:00+00:00",
        "until": "1w",
        "q": "strom",
        "limit": "50",
    }


def test_list_mail_changes_pages():
    change = {
        "at": "2026-09-02T10:00:00Z",
        "channel": "api",
        "source": "llm",
        "field": "categories",
        "old": [],
        "new": ["Rechnungen"],
        "requested": None,
    }
    path = f"{BASE}/mails/a/changes"
    server = FakeServer({("GET", path): paged([[change], [change]])})
    changes = list(server.client().list_mail_changes("a", limit=1))
    assert [c.new for c in changes] == [["Rechnungen"], ["Rechnungen"]]
    assert changes[0].at.tzinfo is not None
    assert server.requests[0].url.params["limit"] == "1"


def test_get_mail_parses_nested_models():
    attachment = {
        "id": "1",
        "filename": "r.pdf",
        "size": 10,
        "mimeType": "application/pdf",
        "inline": False,
    }
    detail = fake_mail(
        "a",
        bodyCleanMarkdown="Hallo",
        bodyCleanVersion=1,
        images=[{"id": "i1", "kind": "inline", "url": "https://x/i1"}],
        to=[{"name": "Me", "email": "me@example.org"}],
        cc=[],
        attachments=[attachment],
    )
    server = FakeServer({("GET", f"{BASE}/mails/a"): httpx.Response(200, json=detail)})
    mail = server.client().get_mail("a", max_body_chars=100)
    assert isinstance(mail, MailDetail)
    assert mail.to[0].email == "me@example.org"
    assert mail.attachments[0].mime_type == "application/pdf"
    assert mail.images[0].alt is None and mail.raw is None
    assert mail.body_markdown is None and mail.body_clean_version == 1
    assert server.requests[0].url.params["maxBodyChars"] == "100"


def test_list_full_mails_gets_each_listed_mail_lazily():
    pages = [[fake_mail("a"), fake_mail("b")]]
    detail = fake_mail(
        "a", bodyCleanMarkdown="Hallo", bodyCleanVersion=1, images=[], to=[], cc=[]
    )
    detail["attachments"] = []
    server = FakeServer(
        {
            ("GET", f"{BASE}/mails"): paged(pages),
            ("GET", f"{BASE}/mails/a"): httpx.Response(200, json=detail),
            ("GET", f"{BASE}/mails/b"): httpx.Response(200, json=detail),
        }
    )
    mails = server.client().list_full_mails(category="none")
    first = next(mails)
    assert isinstance(first, MailDetail) and first.body_clean_markdown == "Hallo"
    paths = [r.url.path for r in server.requests]
    assert paths == [f"{BASE}/mails", f"{BASE}/mails/a"]
    assert server.requests[0].url.params["category"] == "none"
    assert len(list(mails)) == 1
    assert server.requests[-1].url.path == f"{BASE}/mails/b"


def test_categorize_mail_sends_categorized_by():
    answer = fake_mail("a", categories=["Rechnungen"], droppedCategories=[])
    path = f"{BASE}/mails/a/categories"
    server = FakeServer({("PUT", path): httpx.Response(200, json=answer)})
    result = server.client().categorize_mail(
        "a", ["Rechnungen"], categorized_by="mailclient_sort"
    )
    assert isinstance(result, Categorized) and result.categories == ["Rechnungen"]
    assert json.loads(server.requests[0].content) == {
        "categories": ["Rechnungen"],
        "categorizedBy": "mailclient_sort",
    }


def test_update_category_sends_only_given_fields_and_null():
    category = {
        "name": "Strom",
        "color": "amber",
        "description": None,
        "parent": None,
        "mailCount": 0,
        "unreadCount": 0,
    }
    server = FakeServer(
        {("PATCH", f"{BASE}/categories"): httpx.Response(200, json=category)}
    )
    server.client().update_category("Rechnungen/Strom", parent=None)
    assert json.loads(server.requests[0].content) == {
        "category": "Rechnungen/Strom",
        "parent": None,
    }


def test_create_draft_maps_body():
    draft = {"draftKey": "d1", "mail": None, "url": "https://x/d1"}
    server = FakeServer({("POST", f"{BASE}/drafts"): httpx.Response(201, json=draft)})
    result = server.client().create_draft("Danke", reply_to="a")
    assert result.draft_key == "d1"
    assert json.loads(server.requests[0].content) == {"body": "Danke", "replyTo": "a"}


def test_binary_downloads_return_bytes():
    path = f"{BASE}/mails/a/attachments/1"
    server = FakeServer({("GET", path): httpx.Response(200, content=b"%PDF")})
    assert server.client().download_attachment("a", "1") == b"%PDF"


def test_error_answer_raises_with_code():
    error = {
        "message": "Mail nicht gefunden.",
        "error": {"code": "unknown_key", "message": "Mail nicht gefunden."},
    }
    server = FakeServer({("GET", f"{BASE}/mails/x"): httpx.Response(404, json=error)})
    with pytest.raises(MailClientError) as raised:
        server.client().get_mail("x")
    assert raised.value.status == 404 and raised.value.code == "unknown_key"
    assert str(raised.value) == "404 unknown_key: Mail nicht gefunden."


def test_rate_limit_is_retried_after_retry_after(monkeypatch):
    slept = []
    monkeypatch.setattr(mail_serviceapi.time, "sleep", slept.append)
    answers = iter(
        [
            httpx.Response(429, headers={"Retry-After": "2"}, json={"message": "slow"}),
            httpx.Response(200, json=[]),
        ]
    )
    server = FakeServer({("GET", f"{BASE}/categories"): lambda r: next(answers)})
    assert server.client().list_categories() == []
    assert slept == [2]


def test_rate_limit_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(mail_serviceapi.time, "sleep", lambda s: None)
    limited = httpx.Response(
        429,
        headers={"Retry-After": "1"},
        json={"message": "x", "error": {"code": "rate_limited", "message": "x"}},
    )
    server = FakeServer({("GET", f"{BASE}/categories"): limited})
    with pytest.raises(MailClientError) as raised:
        server.client(max_retries=2).list_categories()
    assert raised.value.code == "rate_limited"
    assert len(server.requests) == 3

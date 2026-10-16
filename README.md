# mail_serviceapi

Thin Python client for the mail client's REST service API: one `MailClient`
method per operation, one read-only dataclass per response schema.

```python
from mail_serviceapi import MailClient

with MailClient("https://mail.stephanides.at", token) as client:
    print(client.list_users())
```

- Usage guide: [docs/mail_serviceapi.md](docs/mail_serviceapi.md)
- API reference (generated): [docs/service-api.md](docs/service-api.md)
- Regenerate the reference: `uv run python tools/service_api_docs.py [URL]`
- Tests: `uv run pytest` (`tests/test_spec.py` fails when the client drifts from
  `docs/service-api.openapi.json`)

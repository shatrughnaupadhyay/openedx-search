"""Small HTTP transport; credentials and response bodies never enter errors."""

# Response limits accept primitive integers only, excluding booleans.
# pylint: disable=unidiomatic-typecheck

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .contracts import BackendError


class _NoRedirect(HTTPRedirectHandler):
    """Prevent authentication headers being forwarded to another origin."""

    # urllib requires this override signature.
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # pylint: disable=too-many-positional-arguments
        return None


class HTTPTransport:
    """Authenticated bounded-time HTTP calls to one configured engine."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 10,
        max_response_bytes: int = 16 * 1024 * 1024,
    ):
        """Configure one engine endpoint and bounded request/response limits."""
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.username:
            raise ValueError("Expected an HTTP(S) engine URL without credentials")
        if parts.query or parts.fragment:
            raise ValueError("Engine URL cannot contain a query or fragment")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if type(max_response_bytes) is not int or max_response_bytes < 1:
            raise ValueError("max_response_bytes must be a positive integer")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.opener = build_opener(_NoRedirect())

    def request(self, method, path, *, body=None, engine, ndjson=False):
        """Return decoded JSON; NDJSON imports return one result per line."""
        if engine not in ("meilisearch", "typesense"):
            raise ValueError("Unsupported engine")
        headers = {"Content-Type": "text/plain" if ndjson else "application/json"}
        if engine == "meilisearch":
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            headers["X-TYPESENSE-API-KEY"] = self.api_key
        data = body.encode("utf-8") if ndjson else (json.dumps(body).encode("utf-8") if body is not None else None)
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                content = response.read(self.max_response_bytes + 1)
                if len(content) > self.max_response_bytes:
                    raise BackendError("Engine response exceeds size limit")
                raw = content.decode("utf-8")
        except HTTPError as error:
            error.close()
            raise BackendError(
                f"Engine HTTP status {error.code}",
                retryable=error.code == 429 or error.code >= 500,
            ) from None
        except UnicodeError:
            raise BackendError("Invalid engine response encoding") from None
        except (URLError, TimeoutError, OSError):
            raise BackendError("Engine transport failure", retryable=True) from None
        try:
            return [json.loads(line) for line in raw.splitlines()] if ndjson else json.loads(raw)
        except (ValueError, UnicodeError):
            raise BackendError("Invalid engine response") from None

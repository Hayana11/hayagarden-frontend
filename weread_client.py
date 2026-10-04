"""Minimal server-side adapter for the WeRead Agent gateway.

The browser never talks to this module.  In particular, the API key is only
used to build the upstream Authorization header and is never part of a
returned object or an exception message.
"""

import json
import os
import urllib.error
import urllib.request


GATEWAY_URL = "https://i.weread.qq.com/api/agent/gateway"
SKILL_VERSION = "1.0.4"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class WereadError(Exception):
    def __init__(self, code, status=502):
        self.code = code
        self.status = status
        super().__init__(code)


def _mapped_http_error(status):
    if status in (401, 403):
        return WereadError("WEREAD_UNAUTHORIZED", 502)
    if status == 429:
        return WereadError("WEREAD_RATE_LIMITED", 429)
    if 500 <= status <= 599:
        return WereadError("WEREAD_UPSTREAM_ERROR", 502)
    return WereadError("WEREAD_BAD_RESPONSE", 502)


class WereadClient:
    def __init__(self, api_key=None, opener=None, timeout=8):
        self.api_key = (api_key if api_key is not None else os.environ.get("WEREAD_API_KEY", "")).strip()
        self.opener = opener or urllib.request.build_opener()
        self.timeout = timeout

    def call(self, api_name, params=None):
        if not self.api_key:
            raise WereadError("WEREAD_NOT_CONFIGURED", 503)
        body = json.dumps({
            "api_name": api_name,
            "skill_version": SKILL_VERSION,
            "params": params or {},
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            GATEWAY_URL,
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            response = self.opener.open(request, timeout=self.timeout)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            raise _mapped_http_error(getattr(exc, "code", 0)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise WereadError("WEREAD_UNAVAILABLE", 502) from None
        except Exception:
            # Do not surface arbitrary transport exception strings: a custom
            # transport could accidentally include request headers.
            raise WereadError("WEREAD_UNAVAILABLE", 502) from None

        if status in (401, 403, 429) or status >= 500:
            raise _mapped_http_error(status)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise WereadError("WEREAD_BAD_RESPONSE", 502)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise WereadError("WEREAD_BAD_RESPONSE", 502) from None

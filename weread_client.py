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


def _is_zero_errcode(value):
    if value is None or value is False:
        return True
    if isinstance(value, (int, float)):
        return value == 0
    return str(value).strip() in {"", "0"}


def _gateway_business_error(payload):
    if not isinstance(payload, dict):
        return None
    # upgrade_info is intentionally reduced to a stable status code.  The
    # upstream detail may contain sensitive or implementation-specific data.
    if "upgrade_info" in payload and payload.get("upgrade_info") is not None:
        return WereadError("WEREAD_UPGRADE_REQUIRED", 502)
    if "errcode" in payload and not _is_zero_errcode(payload.get("errcode")):
        return WereadError("WEREAD_BUSINESS_ERROR", 502)
    return None


class WereadClient:
    def __init__(self, api_key=None, opener=None, timeout=8):
        self.api_key = (api_key if api_key is not None else os.environ.get("WEREAD_API_KEY", "")).strip()
        self.opener = opener or urllib.request.build_opener()
        self.timeout = timeout

    def call(self, api_name, params=None):
        if not self.api_key:
            raise WereadError("WEREAD_NOT_CONFIGURED", 503)

        # The WeChat Reading Skill gateway requires business arguments at the
        # top level.  Reserved protocol fields always come from this adapter.
        body_fields = {
            "api_name": api_name,
            "skill_version": SKILL_VERSION,
        }
        if isinstance(params, dict):
            for key, value in params.items():
                if key not in {"api_name", "skill_version"}:
                    body_fields[key] = value
        body = json.dumps(body_fields, ensure_ascii=False).encode("utf-8")
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
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise WereadError("WEREAD_BAD_RESPONSE", 502) from None

        business_error = _gateway_business_error(payload)
        if business_error:
            raise business_error
        return payload

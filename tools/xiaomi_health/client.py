"""Read-only client for Xiaomi Fitness Cloud's CN consumer API."""
from __future__ import annotations

import http.cookiejar
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Any, Mapping

from .crypto import CryptoError, build_encrypted_params, decrypt_response
from .parser import MalformedHealthResponse, parse_series_response
from .store import XiaomiCredentialStore, utc_now


API_BASE = "https://hlth.io.mi.com"
QR_URL = "https://account.xiaomi.com/longPolling/loginUrl"
STS_URL = "https://sts-hlth.io.mi.com/healthapp/sts"
AGGREGATED_PATH = "/app/v1/data/get_aggregated_fitness_data_by_time"
SERVICE_SID = "miothealth"
TIMEOUT_SECONDS = 12
MAX_RESPONSE_BYTES = 1024 * 1024
LOGIN_USER_AGENT = (
    "Dalvik/2.1.0 (Linux; U; Android 12; Xiaomi Build/USER) "
    "APP/mi.health APPV/353001 SDKV/5.3.0.release.68 CPN/com.mi.health PassportSDK/"
)
API_USER_AGENT = "Android-12-3.53.1-Xiaomi-MiHealth"
AUTH_FAILURE_CODES = frozenset({401, 403, 70001, 70002, 70014})
METRICS = frozenset({"steps", "sleep", "heart_rate"})
CST = timezone(timedelta(hours=8))


class XiaomiProviderError(Exception):
    """A stable, secret-free failure code for callers and logs."""

    def __init__(self, code: str):
        self.code = code if code in {"auth_expired", "timeout", "api_error", "malformed_response", "unavailable"} else "api_error"
        super().__init__(self.code)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener():
    return urllib.request.build_opener(_NoRedirect())


def _parse_mi_json(body: bytes) -> Any:
    try:
        text = body.decode("utf-8")
        if text.startswith("&&&START&&&"):
            text = text[len("&&&START&&&"):]
        return json.loads(text)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise XiaomiProviderError("malformed_response") from exc


def _check_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
    except (TypeError, ValueError) as exc:
        raise XiaomiProviderError("api_error") from exc
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "mi.com" or host.endswith(".mi.com") or host == "xiaomi.com" or host.endswith(".xiaomi.com")):
        raise XiaomiProviderError("api_error")
    return value


def _cookie_header(cookies: Mapping[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in cookies.items() if value)


def _cookies(headers: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    values = headers.get_all("Set-Cookie", []) if headers is not None else []
    for value in values:
        pair = str(value).split(";", 1)[0]
        if "=" in pair:
            key, val = pair.split("=", 1)
            if key and key.lower() not in {"path", "expires", "domain", "samesite", "max-age"}:
                out[key] = val
    return out


class XiaomiHealthClient:
    def __init__(self, store: XiaomiCredentialStore, *, opener=None, timeout: float = TIMEOUT_SECONDS):
        self.store = store
        self._opener = opener or _opener()
        self.timeout = timeout
        self.last_diagnostic: dict[str, Any] | None = None

    def _http(self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None, body: bytes | None = None, timeout: float | None = None):
        req = urllib.request.Request(url, data=body, headers=dict(headers or {}), method=method)
        try:
            response = self._opener.open(req, timeout=self.timeout if timeout is None else timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        except (TimeoutError, socket.timeout) as exc:
            raise XiaomiProviderError("timeout") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise XiaomiProviderError("timeout") from exc
            raise XiaomiProviderError("unavailable") from exc
        except Exception as exc:
            if isinstance(exc, (TimeoutError, socket.timeout)):
                raise XiaomiProviderError("timeout") from exc
            raise XiaomiProviderError("unavailable") from exc
        try:
            status = int(getattr(response, "status", getattr(response, "code", 0)) or 0)
            response_headers = getattr(response, "headers", {})
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise XiaomiProviderError("malformed_response")
            return status, response_headers, raw
        except XiaomiProviderError:
            raise
        except Exception as exc:
            raise XiaomiProviderError("unavailable") from exc
        finally:
            try:
                response.close()
            except Exception:
                pass

    def start_qr_login(self) -> dict[str, Any]:
        device_id = "an_" + __import__("secrets").token_hex(16)
        now = int(time.time() * 1000)
        query = urllib.parse.urlencode({
            "_qrsize": "480",
            "qs": "%3Fsid%3Dmiothealth%26_json%3Dtrue",
            "callback": STS_URL,
            "_hasLogo": "false",
            "sid": SERVICE_SID,
            "serviceParam": "",
            "_locale": "zh_CN",
            "_dc": str(now),
        })
        status, headers, body = self._http(
            f"{QR_URL}?{query}",
            headers={"User-Agent": LOGIN_USER_AGENT, "Cookie": f"deviceId={device_id}"},
            timeout=TIMEOUT_SECONDS,
        )
        if status != 200:
            raise XiaomiProviderError("api_error")
        data = _parse_mi_json(body)
        if not isinstance(data, dict) or not isinstance(data.get("loginUrl"), str) or not isinstance(data.get("lp"), str):
            raise XiaomiProviderError("malformed_response")
        data["loginUrl"] = _check_url(data["loginUrl"])
        data["lp"] = _check_url(data["lp"])
        timeout = min(max(int(data.get("timeout") or 300), 1), 300)
        return {
            "login_url": data["loginUrl"],
            "long_polling_url": data["lp"],
            "device_id": device_id,
            "cookies": {"deviceId": device_id, **_cookies(headers)},
            "expires_at": time.time() + timeout,
        }

    def poll_qr_login(self, session: Mapping[str, Any]) -> str:
        if time.time() >= float(session.get("expires_at", 0)):
            return "expired"
        status, headers, body = self._http(
            _check_url(str(session["long_polling_url"])),
            headers={"User-Agent": LOGIN_USER_AGENT, "Cookie": _cookie_header(session.get("cookies") or {})},
            timeout=25,
        )
        if status in {408, 429, 500, 502, 503, 504}:
            return "pending"
        if status != 200:
            raise XiaomiProviderError("api_error")
        data = _parse_mi_json(body)
        if not isinstance(data, dict) or not data.get("ssecurity") or not data.get("userId") or not data.get("location"):
            return "pending"
        cookies = {
            **dict(session.get("cookies") or {}),
            **_cookies(headers),
            "deviceId": str(session["device_id"]),
            "userId": str(data["userId"]),
            "passToken": str(data.get("passToken") or ""),
        }
        location = _check_url(str(data["location"]))
        token_status, token_headers, _ = self._http(
            location,
            headers={"User-Agent": LOGIN_USER_AGENT, "Cookie": _cookie_header(cookies)},
            timeout=TIMEOUT_SECONDS,
        )
        response_cookies = {**cookies, **_cookies(token_headers)}
        service_token = response_cookies.get("serviceToken", "")
        for candidate in (location, token_headers.get("Location", "")):
            try:
                service_token = service_token or urllib.parse.parse_qs(urllib.parse.urlsplit(candidate).query).get("serviceToken", [""])[0]
            except (TypeError, ValueError):
                pass
        if not service_token:
            raise XiaomiProviderError("api_error")
        # STS exchange is best-effort; it does not affect health API credentials.
        sts_query = urllib.parse.urlencode({
            "d": session["device_id"], "ticket": "0", "pwd": "0", "p_ts": int(time.time() * 1000),
            "fid": "0", "p_lm": "2", "p_ur": "CN",
        })
        try:
            self._http(f"{STS_URL}?{sts_query}", headers={"User-Agent": LOGIN_USER_AGENT, "Cookie": _cookie_header(response_cookies)}, timeout=TIMEOUT_SECONDS)
        except XiaomiProviderError:
            pass
        now = utc_now()
        bundle = {
            "user_id": str(data["userId"]),
            "c_user_id": str(data.get("cUserId") or ""),
            "service_token": service_token,
            "ssecurity": str(data["ssecurity"]),
            "pass_token": str(data.get("passToken") or ""),
            "device_id": str(session["device_id"]),
            "auth_state": "valid",
            "updated_at": now,
            "last_checked_at": None,
            "last_success_at": None,
            "last_error": None,
        }
        self.store.save(bundle)
        return "success"

    def _active_bundle(self) -> dict[str, Any]:
        try:
            bundle = self.store.load()
        except Exception as exc:
            raise XiaomiProviderError("unavailable") from exc
        if not bundle:
            raise XiaomiProviderError("unavailable")
        if bundle.get("auth_state") == "auth_expired":
            raise XiaomiProviderError("auth_expired")
        return bundle

    def _request_health(self, bundle: Mapping[str, Any], metric: str, days: int, *, now: datetime | None = None) -> Any:
        today = (now or datetime.now(CST)).astimezone(CST).date()
        start = int(datetime.combine(today - timedelta(days=days - 1), dt_time.min, tzinfo=CST).timestamp())
        end = int(datetime.combine(today, dt_time.max.replace(microsecond=0), tzinfo=CST).timestamp())
        params = {
            "relative_uid": bundle["user_id"],
            "key": metric,
            "tag": "daily_report",
            "start_time": start,
            "end_time": end,
            "limit": days,
        }
        diagnostic: dict[str, Any] = {
            "metric": metric if metric in METRICS else "unknown",
            "endpoint_path": AGGREGATED_PATH,
            "http_status": None,
            "xiaomi_response_code": None,
            "error_class": None,
            "sanitized_error_message": None,
            "response_top_level_keys": [],
            "data_list_present": False,
            "row_count": 0,
        }
        self.last_diagnostic = diagnostic

        def fail(code: str, error_class: str, message: str) -> None:
            diagnostic["error_class"] = error_class
            diagnostic["sanitized_error_message"] = message
            self.last_diagnostic = dict(diagnostic)
            raise XiaomiProviderError(code)

        encrypted = build_encrypted_params("GET", AGGREGATED_PATH, bundle["ssecurity"], params)
        url = f"{API_BASE}{AGGREGATED_PATH}?{urllib.parse.urlencode(encrypted)}"
        try:
            status, _, body = self._http(url, headers={
                "User-Agent": API_USER_AGENT,
                "region_tag": "cn",
                "handleparams": "true",
                "Cookie": _cookie_header({"cUserId": bundle["c_user_id"], "serviceToken": bundle["service_token"]}),
            })
        except XiaomiProviderError as exc:
            fail(exc.code, "TransportError", exc.code)
        diagnostic["http_status"] = status
        if status in {401, 403}:
            fail("auth_expired", "HTTPStatusError", f"HTTP {status}")
        if status != 200:
            fail("api_error", "HTTPStatusError", f"HTTP {status}")
        try:
            result = decrypt_response(bundle["ssecurity"], encrypted["_nonce"], body.decode("utf-8"))
        except (CryptoError, UnicodeError):
            fail("malformed_response", "ResponseDecryptError", "response decrypt failed")
        if not isinstance(result, dict):
            fail("malformed_response", "MalformedResponse", "invalid response envelope")
        diagnostic["response_top_level_keys"] = sorted(
            key if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key) else "<nonstandard>"
            for key in result
            if isinstance(key, str)
        )
        try:
            code = int(result.get("code", -1))
        except (TypeError, ValueError):
            fail("malformed_response", "MalformedResponse", "invalid response code")
        diagnostic["xiaomi_response_code"] = code
        envelope = result.get("result")
        if isinstance(envelope, dict):
            data_list = envelope.get("data_list")
            diagnostic["data_list_present"] = "data_list" in envelope
            diagnostic["row_count"] = len(data_list) if isinstance(data_list, list) else 0
        if code != 0:
            if code in AUTH_FAILURE_CODES:
                fail("auth_expired", "XiaomiResponseError", f"Xiaomi response code {code}")
            fail("api_error", "XiaomiResponseError", f"Xiaomi response code {code}")
        parsed = {"result": envelope}
        try:
            records = parse_series_response(parsed, metric, days=days)
        except MalformedHealthResponse:
            fail("malformed_response", "MalformedHealthResponse", "malformed health rows")
        self.last_diagnostic = dict(diagnostic)
        return records

    def get_series(self, metric: str, days: int) -> dict[str, Any]:
        if metric not in METRICS or not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 30:
            raise XiaomiProviderError("malformed_response")
        bundle = self._active_bundle()
        try:
            records = self._request_health(bundle, metric, days)
        except XiaomiProviderError as exc:
            try:
                self.store.update_status(auth_state="auth_expired" if exc.code == "auth_expired" else None, error=exc.code)
            except Exception:
                pass
            raise
        try:
            self.store.update_status(success=True)
        except Exception:
            # A healthy API response remains useful; status persistence is best-effort.
            pass
        return {
            "status": "PASS" if records else "EMPTY",
            "provider": "xiaomi_fitness_cloud",
            "metric": metric,
            "days": days,
            "records": records,
        }

    def get_latest(self) -> dict[str, Any]:
        metrics = {}
        for metric in ("steps", "sleep", "heart_rate"):
            series = self.get_series(metric, 2)
            metrics[metric] = series["records"][-1] if series["records"] else None
        sampled = [row["sampledAt"] for row in metrics.values() if row]
        dates = [row["dataDate"] for row in metrics.values() if row]
        return {
            "provider": "xiaomi_fitness_cloud",
            "sampledAt": max(sampled) if sampled else None,
            "dataDate": max(dates) if dates else None,
            **metrics,
        }

    def health_status(self) -> dict[str, Any]:
        return self.store.status()


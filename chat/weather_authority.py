"""Shared weather authority for Chat Reality Context and Dash.

Single Open-Meteo fetch + WMO weather_code → Chinese text mapping.
No daemon, cache service, or weather DB.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

# Jilin City — same coords as app/src/config.ts WEATHER_COORDS.
WEATHER_LATITUDE = 43.8378
WEATHER_LONGITUDE = 126.5494
WEATHER_LOCATION_NAME = '吉林市'
WEATHER_TIMEZONE = 'Asia/Shanghai'
WEATHER_SOURCE = 'open-meteo'

_SHANGHAI = ZoneInfo(WEATHER_TIMEZONE)

WeatherFetcher = Callable[[], 'WeatherSnapshot']


@dataclass(frozen=True)
class WeatherSnapshot:
    location: str
    temperature_c: int
    humidity_pct: int
    weather_code: int
    weather_text: str
    observed_at: str
    fetched_at: str
    source: str = WEATHER_SOURCE

    def as_api_dict(self) -> dict[str, Any]:
        return {
            'ok': True,
            'location': self.location,
            'temperature_c': int(self.temperature_c),
            'humidity_pct': int(self.humidity_pct),
            'weather_code': int(self.weather_code),
            'weather_text': self.weather_text,
            'observed_at': self.observed_at,
            'fetched_at': self.fetched_at,
            'source': self.source,
        }


def weather_text_for_code(code: int) -> str:
    """WMO weather interpretation codes → short Chinese labels (Dash+Chat shared)."""
    c = int(code)
    if c == 0:
        return '晴'
    if c <= 2:
        return '多云'
    if c == 3:
        return '阴'
    if c <= 48:
        return '雾'
    if c <= 57:
        return '毛毛雨'
    if c <= 67:
        return '雨'
    if c <= 77:
        return '雪'
    if c <= 82:
        return '阵雨'
    if c <= 86:
        return '阵雪'
    return '雷雨'


def weather_icon_for_code(code: int) -> str:
    """Optional display icon; Chat model context does not require this."""
    c = int(code)
    if c == 0:
        return '☀'
    if c <= 2:
        return '⛅'
    if c == 3:
        return '☁'
    if c <= 48:
        return '🌫'
    if c <= 57:
        return '🌦'
    if c <= 67:
        return '🌧'
    if c <= 77:
        return '❄'
    if c <= 82:
        return '🌧'
    if c <= 86:
        return '❄'
    return '⛈'


def _shanghai_now(now: Optional[datetime] = None) -> datetime:
    if now is None:
        return datetime.now(_SHANGHAI)
    if now.tzinfo is None:
        return now.replace(tzinfo=_SHANGHAI)
    return now.astimezone(_SHANGHAI)


def fetch_weather_now(
    *,
    timeout_sec: float = 4.0,
    now: Optional[datetime] = None,
) -> WeatherSnapshot:
    """Fetch current weather from Open-Meteo. Raises on any failure (fail-closed)."""
    params = urllib.parse.urlencode({
        'latitude': WEATHER_LATITUDE,
        'longitude': WEATHER_LONGITUDE,
        'current': 'temperature_2m,relative_humidity_2m,weather_code',
        'timezone': WEATHER_TIMEZONE,
    })
    url = f'https://api.open-meteo.com/v1/forecast?{params}'
    req = urllib.request.Request(url, headers={'User-Agent': 'HayaGarden/1.0'})
    with urllib.request.urlopen(req, timeout=float(timeout_sec)) as resp:
        raw = resp.read()
    data = json.loads(raw.decode('utf-8'))
    current = data.get('current')
    if not isinstance(current, dict):
        raise ValueError('open-meteo missing current block')
    temp = current.get('temperature_2m')
    hum = current.get('relative_humidity_2m')
    code = current.get('weather_code')
    if temp is None or hum is None or code is None:
        raise ValueError('open-meteo incomplete current fields')
    observed = str(current.get('time') or '').strip()
    fetched = _shanghai_now(now).strftime('%Y-%m-%d %H:%M')
    if not observed:
        observed = fetched
    else:
        # Open-Meteo returns ISO local wall time under timezone=Asia/Shanghai.
        observed = observed.replace('T', ' ')[:16]
    code_i = int(code)
    return WeatherSnapshot(
        location=WEATHER_LOCATION_NAME,
        temperature_c=int(round(float(temp))),
        humidity_pct=int(round(float(hum))),
        weather_code=code_i,
        weather_text=weather_text_for_code(code_i),
        observed_at=observed,
        fetched_at=fetched,
        source=WEATHER_SOURCE,
    )


def try_fetch_weather_now(
    *,
    timeout_sec: float = 4.0,
    now: Optional[datetime] = None,
    fetcher: Optional[WeatherFetcher] = None,
) -> Optional[WeatherSnapshot]:
    """Fail-closed wrapper: None on any error. Never returns mock weather."""
    try:
        if fetcher is not None:
            snap = fetcher()
        else:
            snap = fetch_weather_now(timeout_sec=timeout_sec, now=now)
        if snap is None:
            return None
        return snap
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
            ValueError, TypeError, json.JSONDecodeError, KeyError):
        return None
    except Exception:
        return None

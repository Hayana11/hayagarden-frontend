import { http } from './http';
import type { WeatherNow } from '../types';

/** Shared server weather authority (`GET /api/weather/now`). Never invents mock weather. */
export async function fetchWeatherNow(): Promise<WeatherNow> {
  const data = await http.get<{
    ok?: boolean;
    unavailable?: boolean;
    temperature_c?: number;
    humidity_pct?: number;
    weather_code?: number;
    weather_text?: string;
    observed_at?: string;
    location?: string;
  }>('/api/weather/now');
  if (
    !data
    || data.ok === false
    || data.unavailable
    || data.temperature_c == null
    || data.humidity_pct == null
    || data.weather_code == null
    || !(data.weather_text || '').trim()
  ) {
    return { unavailable: true };
  }
  return {
    temp: Math.round(Number(data.temperature_c)),
    hum: Math.round(Number(data.humidity_pct)),
    code: Number(data.weather_code),
    weather_text: String(data.weather_text).trim(),
    observed_at: data.observed_at,
    location: data.location || '吉林市',
  };
}

/** Display-only icon from weather_code. Weather text comes from server only. */
export function weatherIcon(code: number): string {
  if (code === 0) return '☀';
  if (code <= 2) return '⛅';
  if (code === 3) return '☁';
  if (code <= 48) return '🌫';
  if (code <= 57) return '🌦';
  if (code <= 67) return '🌧';
  if (code <= 77) return '❄';
  if (code <= 82) return '🌧';
  if (code <= 86) return '❄';
  return '⛈';
}

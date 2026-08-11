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
  if (!data || data.ok === false || data.unavailable) {
    return { unavailable: true };
  }
  return {
    temp: Math.round(Number(data.temperature_c)),
    hum: Math.round(Number(data.humidity_pct)),
    code: Number(data.weather_code),
    weather_text: data.weather_text,
    observed_at: data.observed_at,
    location: data.location || '吉林市',
  };
}

/** Prefer server-provided weather_text; icon is display-only. */
export function weatherDesc(code: number, weatherText?: string): [string, string] {
  const text = (weatherText || '').trim();
  if (code === 0) return ['☀', text || '晴'];
  if (code <= 2) return ['⛅', text || '多云'];
  if (code === 3) return ['☁', text || '阴'];
  if (code <= 48) return ['🌫', text || '雾'];
  if (code <= 57) return ['🌦', text || '毛毛雨'];
  if (code <= 67) return ['🌧', text || '雨'];
  if (code <= 77) return ['❄', text || '雪'];
  if (code <= 82) return ['🌧', text || '阵雨'];
  if (code <= 86) return ['❄', text || '阵雪'];
  return ['⛈', text || '雷雨'];
}

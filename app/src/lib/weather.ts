import { WEATHER_COORDS } from '../config';
import type { WeatherNow } from '../types';

export async function fetchWeatherNow(): Promise<WeatherNow> {
  try {
    const url =
      `https://api.open-meteo.com/v1/forecast?latitude=${WEATHER_COORDS.latitude}&longitude=${WEATHER_COORDS.longitude}` +
      `&current=temperature_2m,relative_humidity_2m,weather_code&timezone=Asia%2FShanghai`;
    const res = await fetch(url);
    const data = await res.json();
    const c = data.current;
    return { temp: Math.round(c.temperature_2m), hum: Math.round(c.relative_humidity_2m), code: c.weather_code };
  } catch {
    return { temp: 24, hum: 62, code: 2, mock: true };
  }
}

export function weatherDesc(code: number): [string, string] {
  if (code === 0) return ['☀', '晴'];
  if (code <= 2) return ['⛅', '多云'];
  if (code === 3) return ['☁', '阴'];
  if (code <= 48) return ['🌫', '雾'];
  if (code <= 57) return ['🌦', '毛毛雨'];
  if (code <= 67) return ['🌧', '雨'];
  if (code <= 77) return ['❄', '雪'];
  if (code <= 82) return ['🌧', '阵雨'];
  if (code <= 86) return ['❄', '阵雪'];
  return ['⛈', '雷雨'];
}

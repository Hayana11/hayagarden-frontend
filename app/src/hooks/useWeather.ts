import { useEffect, useState } from 'react';
import { fetchWeatherNow } from '../lib/weather';
import type { WeatherNow } from '../types';

export function useWeather(): WeatherNow | null {
  const [weather, setWeather] = useState<WeatherNow | null>(null);
  useEffect(() => {
    let alive = true;
    fetchWeatherNow().then((w) => {
      if (alive) setWeather(w);
    });
    return () => {
      alive = false;
    };
  }, []);
  return weather;
}

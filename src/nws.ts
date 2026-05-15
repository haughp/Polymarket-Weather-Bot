import axios from "axios";
import { warn } from "./colors";

export const LOCATIONS: Record<
  string,
  { lat: number; lon: number; name: string }
> = {
  nyc: { lat: 40.7772, lon: -73.8726, name: "New York City" },
  chicago: { lat: 41.9742, lon: -87.9073, name: "Chicago" },
  miami: { lat: 25.7959, lon: -80.287, name: "Miami" },
  dallas: { lat: 32.8471, lon: -96.8518, name: "Dallas" },
  seattle: { lat: 47.4502, lon: -122.3088, name: "Seattle" },
  atlanta: { lat: 33.6407, lon: -84.4277, name: "Atlanta" }
};

export const NWS_ENDPOINTS: Record<string, string> = {
  nyc: "https://api.weather.gov/gridpoints/OKX/37,39/forecast/hourly",
  chicago: "https://api.weather.gov/gridpoints/LOT/66,77/forecast/hourly",
  miami: "https://api.weather.gov/gridpoints/MFL/106,51/forecast/hourly",
  dallas: "https://api.weather.gov/gridpoints/FWD/87,107/forecast/hourly",
  seattle: "https://api.weather.gov/gridpoints/SEW/124,61/forecast/hourly",
  atlanta: "https://api.weather.gov/gridpoints/FFC/50,82/forecast/hourly"
};

export const STATION_IDS: Record<string, string> = {
  nyc: "KLGA",
  chicago: "KORD",
  miami: "KMIA",
  dallas: "KDAL",
  seattle: "KSEA",
  atlanta: "KATL"
};

const USER_AGENT = "weatherbot-ts/1.0";

export interface DailyForecasts {
  max: Record<string, number>;
  min: Record<string, number>;
}

export async function getForecast(citySlug: string): Promise<DailyForecasts> {
  const forecastUrl = NWS_ENDPOINTS[citySlug];
  const stationId = STATION_IDS[citySlug];
  const dailyMax: Record<string, number> = {};
  const dailyMin: Record<string, number> = {};
  const headers = { "User-Agent": USER_AGENT };

  // Real observations — what already happened today
  try {
    const obsUrl = `https://api.weather.gov/stations/${stationId}/observations?limit=48`;
    const r = await axios.get(obsUrl, { timeout: 10000, headers });
    const features = (r.data?.features ?? []) as any[];
    for (const obs of features) {
      const props = obs.properties ?? {};
      const timeStr = String(props.timestamp ?? "").slice(0, 10);
      const tempC = props.temperature?.value as number | null | undefined;
      if (typeof tempC === "number") {
        const tempF = Math.round((tempC * 9) / 5 + 32);
        if (!(timeStr in dailyMax) || tempF > dailyMax[timeStr]) {
          dailyMax[timeStr] = tempF;
        }
        if (!(timeStr in dailyMin) || tempF < dailyMin[timeStr]) {
          dailyMin[timeStr] = tempF;
        }
      }
    }
  } catch (e) {
    warn(`Observations error for ${citySlug}: ${String(e)}`);
  }

  // Hourly forecast — upcoming hours
  try {
    const r = await axios.get(forecastUrl, { timeout: 10000, headers });
    const periods = r.data?.properties?.periods ?? [];
    for (const p of periods as any[]) {
      const date = String(p.startTime ?? "").slice(0, 10);
      let temp = p.temperature as number;
      if (p.temperatureUnit === "C") {
        temp = Math.round((temp * 9) / 5 + 32);
      }
      if (!(date in dailyMax) || temp > dailyMax[date]) {
        dailyMax[date] = temp;
      }
      if (!(date in dailyMin) || temp < dailyMin[date]) {
        dailyMin[date] = temp;
      }
    }
  } catch (e) {
    warn(`Forecast error for ${citySlug}: ${String(e)}`);
  }

  return { max: dailyMax, min: dailyMin };
}

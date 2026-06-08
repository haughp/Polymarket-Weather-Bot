import axios from "axios";
import { warn } from "./colors";

export const LOCATIONS: Record<
  string,
  { lat: number; lon: number; name: string; tz: string }
> = {
  nyc:           { lat: 40.7772, lon: -73.8726,  name: "New York City", tz: "America/New_York" },
  chicago:       { lat: 41.9742, lon: -87.9073,  name: "Chicago",       tz: "America/Chicago" },
  miami:         { lat: 25.7959, lon: -80.287,   name: "Miami",         tz: "America/New_York" },
  dallas:        { lat: 32.8471, lon: -96.8518,  name: "Dallas",        tz: "America/Chicago" },
  seattle:       { lat: 47.4502, lon: -122.3088, name: "Seattle",       tz: "America/Los_Angeles" },
  atlanta:       { lat: 33.6407, lon: -84.4277,  name: "Atlanta",       tz: "America/New_York" },
  // US expansion (added 2026-06-02) — start in shadow mode until promoted (see cityStatus.ts)
  houston:       { lat: 29.7858, lon: -95.3676,  name: "Houston",       tz: "America/Chicago" },
  denver:        { lat: 39.7392, lon: -104.9903, name: "Denver",        tz: "America/Denver" },
  "los-angeles": { lat: 34.0536, lon: -118.2456, name: "Los Angeles",   tz: "America/Los_Angeles" },
  "san-francisco": { lat: 37.7749, lon: -122.4194, name: "San Francisco", tz: "America/Los_Angeles" },
  austin:        { lat: 30.2672, lon: -97.7431,  name: "Austin",        tz: "America/Chicago" }
};

export const NWS_ENDPOINTS: Record<string, string> = {
  nyc:     "https://api.weather.gov/gridpoints/OKX/37,39/forecast",
  chicago: "https://api.weather.gov/gridpoints/LOT/66,77/forecast",
  miami:   "https://api.weather.gov/gridpoints/MFL/106,51/forecast",
  dallas:  "https://api.weather.gov/gridpoints/FWD/87,107/forecast",
  seattle: "https://api.weather.gov/gridpoints/SEW/124,61/forecast",
  atlanta: "https://api.weather.gov/gridpoints/FFC/50,82/forecast",
  houston: "https://api.weather.gov/gridpoints/HGX/63,96/forecast",
  denver:  "https://api.weather.gov/gridpoints/BOU/63,62/forecast",
  "los-angeles": "https://api.weather.gov/gridpoints/LOX/155,45/forecast",
  "san-francisco": "https://api.weather.gov/gridpoints/MTR/85,105/forecast",
  austin:  "https://api.weather.gov/gridpoints/EWX/156,91/forecast"
};

export const NWS_HOURLY_ENDPOINTS: Record<string, string> = {
  nyc:     "https://api.weather.gov/gridpoints/OKX/37,39/forecast/hourly",
  chicago: "https://api.weather.gov/gridpoints/LOT/66,77/forecast/hourly",
  miami:   "https://api.weather.gov/gridpoints/MFL/106,51/forecast/hourly",
  dallas:  "https://api.weather.gov/gridpoints/FWD/87,107/forecast/hourly",
  seattle: "https://api.weather.gov/gridpoints/SEW/124,61/forecast/hourly",
  atlanta: "https://api.weather.gov/gridpoints/FFC/50,82/forecast/hourly",
  houston: "https://api.weather.gov/gridpoints/HGX/63,96/forecast/hourly",
  denver:  "https://api.weather.gov/gridpoints/BOU/63,62/forecast/hourly",
  "los-angeles": "https://api.weather.gov/gridpoints/LOX/155,45/forecast/hourly",
  "san-francisco": "https://api.weather.gov/gridpoints/MTR/85,105/forecast/hourly",
  austin:  "https://api.weather.gov/gridpoints/EWX/156,91/forecast/hourly"
};

export const STATION_IDS: Record<string, string> = {
  nyc: "KLGA",
  chicago: "KORD",
  miami: "KMIA",
  dallas: "KDAL",
  seattle: "KSEA",
  atlanta: "KATL",
  houston: "KIAH",
  denver: "KDEN",
  "los-angeles": "KLAX",
  "san-francisco": "KSFO",
  austin: "KAUS"
};

// Per-city, per-direction NWS forecast bias correction.
// Convention: bias = (expected_actual − nws_forecast).
//   Positive → NWS runs cold (actual warmer than forecast) → add to push bucket selection up.
//   Negative → NWS runs warm (actual cooler than forecast) → subtract to push bucket selection down.
//
// Values from 30-day GFS-vs-ERA5 backtest (May 2026, 25–28 resolved markets per cell).
// GFS is the closest free proxy for NWS at 24-48h lead. NWS/MOS post-processing may shift
// these by 0.5-1°F in either direction — treat as provisional until ≥20 Polymarket-resolved
// trades per cell are available.
//
// Min-temp cells with 0 entries below had <10 resolved markets in the backtest window and
// default to no correction until confirmed.
export const FORECAST_BIAS: Record<string, { highest: number; lowest: number }> = {
  //             highest (daily max)  lowest (daily min)
  nyc:     { highest: +0.6, lowest: +0.7 },  // GFS runs cold for both metrics
  chicago: { highest: -0.5, lowest:  0.0 },  // GFS warm bias on max; min untested
  miami:   { highest:  0.0, lowest: -0.6 },  // GFS near-neutral max; warm bias on min
  dallas:  { highest: -1.2, lowest:  0.0 },  // Strong GFS warm bias on max; min untested
  seattle: { highest: +0.6, lowest:  0.0 },  // GFS cold bias on max; min untested
  atlanta: { highest: -0.3, lowest:  0.0 },  // Mild GFS warm bias on max; min untested
  // US expansion (2026-06-02) — no correction until ≥10 resolved shadow samples calibrate each cell
  houston:         { highest: 0.0, lowest: 0.0 },
  denver:          { highest: 0.0, lowest: 0.0 },
  "los-angeles":   { highest: 0.0, lowest: 0.0 },
  "san-francisco": { highest: 0.0, lowest: 0.0 },
  austin:          { highest: 0.0, lowest: 0.0 },
};

const USER_AGENT = "weatherbot-ts/1.0";

export interface DailyForecasts {
  max: Record<string, number>;
  min: Record<string, number>;
  maxTime: Record<string, string>;
  minTime: Record<string, string>;
}

export async function getForecast(citySlug: string): Promise<DailyForecasts> {
  const forecastUrl = NWS_ENDPOINTS[citySlug];
  const hourlyUrl = NWS_HOURLY_ENDPOINTS[citySlug];
  const stationId = STATION_IDS[citySlug];
  const dailyMax: Record<string, number> = {};
  const dailyMin: Record<string, number> = {};
  const dailyMaxTime: Record<string, string> = {};
  const dailyMinTime: Record<string, string> = {};
  const headers = { "User-Agent": USER_AGENT };

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
          dailyMaxTime[timeStr] = String(props.timestamp ?? "");
        }
        if (!(timeStr in dailyMin) || tempF < dailyMin[timeStr]) {
          dailyMin[timeStr] = tempF;
          dailyMinTime[timeStr] = String(props.timestamp ?? "");
        }
      }
    }
  } catch (e) {
    warn(`Observations error for ${citySlug}: ${String(e)}`);
  }

  // Calibrated 12-hour-period forecast — authoritative source for daily High/Low.
  // Each period has isDaytime + a single temperature value (HIGH if daytime, LOW if not).
  // Polymarket "daily low for date D" = pre-dawn low on D in city local time, which is the
  // night period whose endTime falls on D.
  try {
    const r = await axios.get(forecastUrl, { timeout: 10000, headers });
    const periods = r.data?.properties?.periods ?? [];
    for (const p of periods as any[]) {
      let temp = p.temperature as number;
      if (p.temperatureUnit === "C") {
        temp = Math.round((temp * 9) / 5 + 32);
      }
      if (p.isDaytime) {
        const date = String(p.startTime ?? "").slice(0, 10);
        if (date) dailyMax[date] = temp;
      } else {
        const date = String(p.endTime ?? "").slice(0, 10);
        if (date) dailyMin[date] = temp;
      }
    }
  } catch (e) {
    warn(`Forecast error for ${citySlug}: ${String(e)}`);
  }

  // Hourly endpoint used ONLY to identify the peak hour for the entry-window gate.
  // Temperature magnitudes from /forecast above remain authoritative.
  try {
    const r = await axios.get(hourlyUrl, { timeout: 10000, headers });
    const periods = r.data?.properties?.periods ?? [];
    const perDateHi: Record<string, { t: number; ts: string }> = {};
    const perDateLo: Record<string, { t: number; ts: string }> = {};
    for (const p of periods as any[]) {
      const date = String(p.startTime ?? "").slice(0, 10);
      if (!date) continue;
      let t = p.temperature as number;
      if (p.temperatureUnit === "C") {
        t = Math.round((t * 9) / 5 + 32);
      }
      const cur = perDateHi[date];
      if (!cur || t > cur.t) perDateHi[date] = { t, ts: String(p.startTime ?? "") };
      const curLo = perDateLo[date];
      if (!curLo || t < curLo.t) perDateLo[date] = { t, ts: String(p.startTime ?? "") };
    }
    for (const d in perDateHi) {
      if (!(d in dailyMaxTime)) dailyMaxTime[d] = perDateHi[d].ts;
    }
    for (const d in perDateLo) {
      if (!(d in dailyMinTime)) dailyMinTime[d] = perDateLo[d].ts;
    }
  } catch (e) {
    warn(`Hourly peak-time error for ${citySlug}: ${String(e)}`);
  }

  return { max: dailyMax, min: dailyMin, maxTime: dailyMaxTime, minTime: dailyMinTime };
}

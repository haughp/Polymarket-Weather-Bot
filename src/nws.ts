import axios from "axios";
import { warn } from "./colors";
import { getProvider } from "./matrix";

// Per-city forecast provider — FALLBACK ONLY. provider_matrix.json supersedes this per
// (city, mode) when present (see matrix.ts getProvider); this dict is the safety net used
// when the matrix is missing or lacks a cell. "nws" = NWS API. "ecmwf" = Open-Meteo ECMWF
// IFS 0.25° (aliased to ecmwf_ifs025 at fetch time).
export const FORECAST_PROVIDER: Record<string, "nws" | "ecmwf"> = {
  nyc:   "ecmwf", // #1 on NYC max in 30d station backtest (debiased MAE 1.79); confirmed the Jun-10 flip
  miami: "ecmwf", // #1 on Miami min over 89d vs KMIA CLI (MAE 1.26, bias ~0); NWS/NBM warm-biased on min
};

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
  houston:       { lat: 29.6375, lon: -95.2825,  name: "Houston",       tz: "America/Chicago" },   // KHOU (Hobby) — Polymarket resolves here, NOT KIAH
  denver:        { lat: 39.7133, lon: -104.7581, name: "Denver",        tz: "America/Denver" },    // KBKF (Buckley SFB, Aurora) — Polymarket resolves here, NOT KDEN
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
  houston: "https://api.weather.gov/gridpoints/HGX/66,89/forecast",
  denver:  "https://api.weather.gov/gridpoints/BOU/71,60/forecast",
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
  houston: "https://api.weather.gov/gridpoints/HGX/66,89/forecast/hourly",
  denver:  "https://api.weather.gov/gridpoints/BOU/71,60/forecast/hourly",
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
  houston: "KHOU",
  denver: "KBKF",
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
  // nyc/miami biases are for ECMWF IFS (their provider above), from the 1-day-lead
  // backtest vs official CLI station actuals (provider_backtest.py, 30d/90d Jun 2026).
  nyc:     { highest: +1.5, lowest: +1.0 },  // ECMWF cold on KLGA: +1.55/90d, +1.57/30d max; +1.27/90d, +0.90/30d min
  miami:   { highest: +4.0, lowest:  0.0 },  // ECMWF grid cell far cooler than KMIA on max (+3.7/90d, +4.4/30d); min unbiased (+0.06/90d)
  chicago: { highest:  0.0, lowest:  0.0 },  // Reset to 0.0 — May GFS ≠ June NWS; recalibrating with live trades
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

async function getNwsForecast(citySlug: string): Promise<DailyForecasts> {
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

// ── ECMWF IFS 0.25° via Open-Meteo ───────────────────────────────────────────
// Mirrors ecmwf_forecast_pipeline.py:fetch_ecmwf_daily_and_peak.
// Returns the same DailyForecasts shape so strategy.ts is unchanged.

const OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast";

// Legacy alias: FORECAST_PROVIDER (and old configs) may say "ecmwf" — map it to the
// concrete Open-Meteo model the provider_matrix uses for ECMWF IFS.
const ECMWF_ALIAS = "ecmwf_ifs025";

// Result of fetching ONE mode (max or min) from a single Open-Meteo model.
interface ModeForecast {
  vals: Record<string, number>;  // dateStr → temperature (°F)
  time: Record<string, string>;  // dateStr → ISO timestamp of the peak/trough hour
}

// Fetch a single mode (max|min) for one city from a specific Open-Meteo model.
// Returns the per-mode slice; strategy.ts only ever consumes the merged DailyForecasts.
// Exported (and indirected via the `openMeteoFetcher` binding below) so unit tests can
// stub the network without hitting Open-Meteo.
export async function fetchOpenMeteoMode(
  citySlug: string,
  mode: "max" | "min",
  model: string
): Promise<ModeForecast> {
  const loc = LOCATIONS[citySlug];
  if (!loc) return { vals: {}, time: {} };

  const vals: Record<string, number> = {};
  const time: Record<string, string> = {};
  const dailyField = mode === "max" ? "temperature_2m_max" : "temperature_2m_min";

  try {
    const r = await axios.get(OPEN_METEO_FORECAST, {
      timeout: 15000,
      params: {
        latitude:         loc.lat,
        longitude:        loc.lon,
        daily:            dailyField,
        hourly:           "temperature_2m",
        temperature_unit: "fahrenheit",
        timezone:         loc.tz,
        models:           model === "ecmwf" ? ECMWF_ALIAS : model,
        forecast_days:    7,
      },
    });

    const daily      = r.data?.daily ?? {};
    const dailyTimes = (daily.time ?? []) as string[];
    const dailyVals  = (daily[dailyField] ?? []) as (number | null)[];

    const hourly     = r.data?.hourly ?? {};
    const hTimes     = (hourly.time ?? []) as string[];
    const hVals      = (hourly.temperature_2m ?? []) as (number | null)[];

    for (let i = 0; i < dailyTimes.length; i++) {
      const dateStr = dailyTimes[i];
      const val     = dailyVals[i];
      if (!dateStr || val == null) continue;

      vals[dateStr] = val;

      // Find peak hour on this date from the hourly series, convert to UTC ISO.
      const dayHours = hTimes
        .map((t, j) => ({ t, v: hVals[j] }))
        .filter(x => x.t.startsWith(dateStr) && x.v != null) as { t: string; v: number }[];

      if (dayHours.length > 0) {
        const peak = mode === "max"
          ? dayHours.reduce((a, b) => b.v > a.v ? b : a)
          : dayHours.reduce((a, b) => b.v < a.v ? b : a);

        // Open-Meteo returns local ISO without timezone — attach tz offset then convert to UTC.
        // Node's Intl can resolve the IANA offset; simplest portable approach: build a Date
        // by treating the local time as UTC offset from Intl.DateTimeFormat.
        try {
          const localStr = peak.t; // "2026-06-12T14:00"
          // Get UTC offset in minutes for this city at this instant
          const probe    = new Date(localStr + "Z"); // treat as UTC first
          const parts    = new Intl.DateTimeFormat("en-US", {
            timeZone: loc.tz, hour12: false,
            year: "numeric", month: "2-digit", day: "2-digit",
            hour: "2-digit", minute: "2-digit",
          }).formatToParts(probe);
          const get = (type: string) => parts.find(p => p.type === type)?.value ?? "00";
          const cityYMD  = `${get("year")}-${get("month")}-${get("day")}`;
          const cityHM   = `${get("hour").replace("24", "00")}:${get("minute")}`;
          const cityDate = new Date(`${cityYMD}T${cityHM}Z`);
          const offsetMs = probe.getTime() - cityDate.getTime();
          const utcDate  = new Date(new Date(localStr).getTime() + offsetMs);
          time[dateStr] = utcDate.toISOString();
        } catch {
          // Fall back to local ISO string if UTC conversion fails — strategy.ts
          // only uses this for the hours-to-peak gate, so a small offset is harmless.
          time[dateStr] = peak.t;
        }
      }
    }
  } catch (e) {
    warn(`Open-Meteo error for ${citySlug} ${mode} (${model}): ${String(e)}`);
  }

  return { vals, time };
}

// Fetch a single mode from NWS by reusing the full NWS forecast and slicing it.
// (NWS returns both max and min in one shot; per-mode callers just take what they need.)
async function fetchNwsMode(citySlug: string, mode: "max" | "min"): Promise<ModeForecast> {
  const f = await getNwsForecast(citySlug);
  return mode === "max"
    ? { vals: f.max, time: f.maxTime }
    : { vals: f.min, time: f.minTime };
}

// Indirection seam so unit tests can stub forecast fetching without live HTTP.
// Tests call __setFetchersForTest(...) to override and __restoreFetchers() to reset.
const _realOpenMeteoFetcher = fetchOpenMeteoMode;
const _realNwsFetcher = fetchNwsMode;
export let openMeteoFetcher = fetchOpenMeteoMode;
export let nwsFetcher = fetchNwsMode;
export function __setFetchersForTest(om: typeof fetchOpenMeteoMode, nws: typeof fetchNwsMode) {
  openMeteoFetcher = om;
  nwsFetcher = nws;
}
export function __restoreFetchers() {
  openMeteoFetcher = _realOpenMeteoFetcher;
  nwsFetcher = _realNwsFetcher;
}

// Resolve one mode to its matrix-chosen provider and fetch it from the right source.
async function getModeForecast(citySlug: string, mode: "max" | "min"): Promise<ModeForecast> {
  const provider = getProvider(citySlug, mode); // matrix → FORECAST_PROVIDER → "nws"
  return provider === "nws"
    ? nwsFetcher(citySlug, mode)
    : openMeteoFetcher(citySlug, mode, provider);
}

// ── Public router ────────────────────────────────────────────────────────────
// strategy.ts imports and calls getForecast(citySlug) — this is that function.
// It resolves the forecast provider PER MODE from the provider matrix (a city's best
// max model and best min model can differ, and either can be NWS), fetches each mode
// from its own source, and merges them into one DailyForecasts so strategy.ts is
// unchanged. Falls back to FORECAST_PROVIDER → "nws" when the matrix lacks a cell.
export async function getForecast(citySlug: string): Promise<DailyForecasts> {
  const [max, min] = await Promise.all([
    getModeForecast(citySlug, "max"),
    getModeForecast(citySlug, "min"),
  ]);
  return {
    max:     max.vals,
    min:     min.vals,
    maxTime: max.time,
    minTime: min.time,
  };
}

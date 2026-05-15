import axios from "axios";
import { warn } from "./colors";

export async function getMarketResolution(marketId: string): Promise<boolean | null> {
  const url = `https://gamma-api.polymarket.com/markets/${marketId}`;
  try {
    const r = await axios.get(url, { timeout: 5000 });
    if (r.data?.closed || r.data?.resolved || r.data?.isResolved) {
      const winner = String(r.data?.winner || r.data?.result || "").toLowerCase();
      if (winner === "yes" || winner === "1") return true;
      if (winner === "no" || winner === "0") return false;
    }
    return null;
  } catch {
    return null;
  }
}

export interface PolymarketEvent {
  id: string;
  endDate?: string;
  end_date_iso?: string;
  markets?: PolymarketMarket[];
  [key: string]: any;
}

export interface PolymarketMarket {
  id: string;
  question: string;
  outcomePrices?: string;
  /** JSON string array of CLOB conditional token IDs [Yes, No] for binary markets */
  clobTokenIds?: string;
  [key: string]: any;
}

/** First outcome token (Yes on standard Yes/No markets). */
export function getYesTokenId(market: PolymarketMarket): string | null {
  const raw = market.clobTokenIds;
  if (!raw) return null;
  try {
    const ids = typeof raw === "string" ? (JSON.parse(raw) as unknown) : raw;
    if (Array.isArray(ids) && ids.length > 0) {
      return String(ids[0]);
    }
  } catch {
    return null;
  }
  return null;
}

export async function getPolymarketEvent(
  citySlug: string,
  month: string,
  day: number,
  year: number,
  mode: "highest" | "lowest" = "highest"
): Promise<PolymarketEvent | null> {
  const slug = `${mode}-temperature-in-${citySlug}-on-${month}-${day}-${year}`;
  const url = `https://gamma-api.polymarket.com/events?slug=${slug}`;
  try {
    const r = await axios.get(url, { timeout: 10000 });
    const data = r.data;
    if (Array.isArray(data) && data.length > 0) {
      return data[0] as PolymarketEvent;
    }
  } catch (e) {
    warn(`Polymarket API error: ${String(e)}`);
  }
  return null;
}

export async function getMarketYesPrice(marketId: string): Promise<number | null> {
  const url = `https://gamma-api.polymarket.com/markets/${marketId}`;
  try {
    const r = await axios.get(url, { timeout: 5000 });
    const pricesStr = r.data?.outcomePrices ?? "[0.5,0.5]";
    const prices = JSON.parse(pricesStr) as number[];
    const currentPrice = Number(prices[0]);
    return isFinite(currentPrice) ? currentPrice : null;
  } catch {
    return null;
  }
}

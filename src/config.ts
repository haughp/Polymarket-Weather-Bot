/** Wallet signature type: 0 = EOA, 1 = Polymarket proxy (Magic), 2 = Gnosis Safe */
export type SignatureType = 0 | 1 | 2;

export interface BotConfig {
  entry_threshold: number;
  exit_threshold: number;
  max_trades_per_run: number;
  max_open_positions: number;
  locations: string;
  polymarket_private_key: string;
  polymarket_proxy_wallet_address: string;
  /** Use proxy/safe wallet (funds at proxy address). If true, signature_type defaults to 2. */
  use_proxy_wallet: boolean;
  /** 0 = EOA, 1 = Polymarket proxy, 2 = Gnosis Safe. When use_proxy_wallet=true default is 2. */
  signature_type: SignatureType;
}

export const DEFAULT_CONFIG: BotConfig = {
  entry_threshold: 0.15,
  exit_threshold: 0.45,
  max_trades_per_run: 5,
  max_open_positions: 10,
  locations: "nyc,chicago,miami,dallas,seattle,atlanta,houston,denver,los-angeles,san-francisco,austin",
  polymarket_private_key: "",
  polymarket_proxy_wallet_address: "",
  use_proxy_wallet: false,
  signature_type: 0
};

export async function loadConfig(): Promise<BotConfig> {
  const parseNumber = (value: string | undefined, fallback: number): number => {
    if (value === undefined) return fallback;
    const n = Number(value);
    return Number.isFinite(n) ? n : fallback;
  };

  return {
    entry_threshold: parseNumber(
      process.env.ENTRY_THRESHOLD,
      DEFAULT_CONFIG.entry_threshold
    ),
    exit_threshold: parseNumber(
      process.env.EXIT_THRESHOLD,
      DEFAULT_CONFIG.exit_threshold
    ),
    max_trades_per_run: parseNumber(
      process.env.MAX_TRADES_PER_RUN,
      DEFAULT_CONFIG.max_trades_per_run
    ),
    max_open_positions: parseNumber(
      process.env.MAX_OPEN_POSITIONS,
      DEFAULT_CONFIG.max_open_positions
    ),
    locations: process.env.LOCATIONS ?? DEFAULT_CONFIG.locations,
    polymarket_private_key: process.env.POLYMARKET_PRIVATE_KEY || process.env.POLY_PRIVATE_KEY || "",
    polymarket_proxy_wallet_address:
      process.env.POLYMARKET_PROXY_WALLET_ADDRESS || process.env.POLY_FUNDER_ADDRESS || "",
    use_proxy_wallet:
      (process.env.USE_PROXY_WALLET ?? "").toLowerCase() === "true",
    signature_type: (() => {
      const raw = process.env.SIGNATURE_TYPE ?? "";
      if (raw === "0") return 0 as SignatureType;
      if (raw === "1") return 1 as SignatureType;
      if (raw === "2") return 2 as SignatureType;
      return (process.env.USE_PROXY_WALLET ?? "").toLowerCase() === "true"
        ? (2 as SignatureType)
        : (0 as SignatureType);
    })()
  };
}

export function getActiveLocations(cfg: BotConfig): string[] {
  return cfg.locations
    .split(",")
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
}

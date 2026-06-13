import { Wallet } from "@ethersproject/wallet";
import { ClobClient, OrderType, Side, SignatureTypeV2 } from "@polymarket/clob-client-v2";
import axios from "axios";
import { info, ok, warn } from "./colors";
import type { BotConfig } from "./config";

const CLOB_HOST = "https://clob.polymarket.com";
const CHAIN_ID = 137 as const;

/**
 * Authenticated CLOB client (proxy/safe funder from config when applicable).
 */
export function getSignerWallet(cfg: BotConfig): Wallet {
  const pk = cfg.polymarket_private_key.trim();
  return new Wallet(pk.startsWith("0x") ? pk : "0x" + pk);
}

export function getFunderAddress(cfg: BotConfig, wallet?: Wallet): string | undefined {
  // Align with Python weather_executor: Always use the explicitly provided 
  // proxy/funder address if it exists, regardless of the signature_type.
  const proxy = cfg.polymarket_proxy_wallet_address.trim();
  return proxy ? proxy : (wallet?.address || undefined);
}

function hasValidApiCreds(value: unknown): value is {
  key: string;
  secret: string;
  passphrase: string;
} {
  if (!value || typeof value !== "object") return false;
  const maybe = value as Record<string, unknown>;
  return (
    typeof maybe.key === "string" &&
    maybe.key.length > 0 &&
    typeof maybe.secret === "string" &&
    maybe.secret.length > 0 &&
    typeof maybe.passphrase === "string" &&
    maybe.passphrase.length > 0
  );
}

export async function getApiCreds(cfg: BotConfig): Promise<{
  client: ClobClient;
  wallet: Wallet;
  apiCreds: { key: string; secret: string; passphrase: string };
}> {
  const wallet = getSignerWallet(cfg);
  const funder = getFunderAddress(cfg, wallet);
  const signatureType = cfg.signature_type;
  const temp = new ClobClient({
    host: CLOB_HOST,
    chain: CHAIN_ID,
    signer: wallet,
    signatureType: signatureType as SignatureTypeV2,
  });

  let lastError: unknown;
  try {
    info("Attempting to derive CLOB API key from signature...");
    const apiCreds = await temp.deriveApiKey();
    if (hasValidApiCreds(apiCreds)) {
      ok("Successfully derived CLOB API key.");
      return { client: temp, wallet, apiCreds };
    }
    lastError = new Error("deriveApiKey() returned invalid credentials");
  } catch (deriveError) {
    lastError = deriveError;
    warn(`Could not derive key (this is normal on first run): ${String(deriveError)}`);
  }

  try {
    info("Derivation failed. Attempting to create a new CLOB API key...");
    const apiCreds = await temp.createApiKey();
    if (hasValidApiCreds(apiCreds)) {
      ok("Successfully created and registered a new CLOB API key.");
      return { client: temp, wallet, apiCreds };
    }
    throw new Error("createApiKey() returned invalid credentials");
  } catch (createError) {
    const details =
      createError instanceof Error
        ? createError.message
        : String(createError ?? lastError ?? "unknown error");
    throw new Error(
      `Failed to create or derive CLOB API credentials. Check private key, signature type, and proxy/funder address. Last error: ${details}`
    );
  }
}

export async function getClobClient(cfg: BotConfig): Promise<ClobClient> {
  const { wallet, apiCreds } = await getApiCreds(cfg);
  const signatureType = cfg.signature_type;
  const funder = getFunderAddress(cfg, wallet);
  return new ClobClient({
    host: CLOB_HOST,
    chain: CHAIN_ID,
    signer: wallet,
    creds: apiCreds,
    signatureType: signatureType as SignatureTypeV2,
  });
}

/** Returns the current lowest ask (what you pay to buy YES) from the CLOB. */
export async function fetchAskPrice(tokenId: string): Promise<number> {
  const r = await axios.get(`${CLOB_HOST}/price`, {
    params: { token_id: tokenId, side: "sell" },
    timeout: 8000,
    headers: { "User-Agent": "weatherbot-ts/1.0" }
  });
  const price = parseFloat(r.data?.price);
  if (!isFinite(price) || price <= 0) {
    throw new Error(`Invalid ask price for token ${tokenId}: ${JSON.stringify(r.data)}`);
  }
  return price;
}

export async function buyYesFok(
  client: ClobClient,
  tokenId: string,
  limitPrice: number,
  amountUsdc: number
): Promise<{ filled: boolean; orderId: string }> {
  // createAndPostMarketOrder accepts FOK/FAK; `amount` = USDC to spend (BUY side)
  const resp = (await client.createAndPostMarketOrder(
    { tokenID: tokenId, price: limitPrice, side: Side.BUY, amount: amountUsdc },
    undefined,
    OrderType.FOK
  )) as Record<string, unknown> | null;
  // FOK fills completely or cancels — check status field
  const status = String(resp?.status ?? "").toUpperCase();
  const filled = status === "MATCHED" || status === "FILLED" || status === "MTC";
  return { filled, orderId: String(resp?.orderID ?? "") };
}

/** @deprecated Use buyYesFok — GTC orders sit unmatched in thin weather markets. */
export async function buyYesLimit(
  client: ClobClient,
  tokenId: string,
  price: number,
  sizeShares: number
): Promise<unknown> {
  return client.createAndPostOrder(
    {
      tokenID: tokenId,
      price,
      side: Side.BUY,
      size: sizeShares
    },
    undefined,
    OrderType.GTC
  );
}

export async function sellYesLimit(
  client: ClobClient,
  tokenId: string,
  price: number,
  sizeShares: number
): Promise<unknown> {
  return client.createAndPostOrder(
    {
      tokenID: tokenId,
      price,
      side: Side.SELL,
      size: sizeShares
    },
    undefined,
    OrderType.GTC
  );
}

import axios from "axios";
import type { BotConfig } from "./config";
import { getFunderAddress, getSignerWallet } from "./clob";

// Polymarket uses pUSD (V2) for collateral on Polygon
const PUSD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB";
const POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com";

export async function getWalletBalanceUsdViaClob(cfg: BotConfig): Promise<number> {
  try {
    const wallet = getSignerWallet(cfg);
    const address = getFunderAddress(cfg, wallet) || wallet.address;
    
    const response = await axios.post(POLYGON_RPC, {
      jsonrpc: "2.0",
      id: 1,
      method: "eth_call",
      params: [
        {
          to: PUSD_ADDRESS,
          data: "0x70a08231000000000000000000000000" + address.toLowerCase().replace("0x", "")
        },
        "latest"
      ]
    });
    
    const hex = response.data?.result;
    if (hex && hex !== "0x") {
      // pUSD has 6 decimals
      return parseInt(hex, 16) / 1e6;
    }
  } catch (error) {
    console.error("Error fetching pUSD balance from Polygon:", error);
  }
  return 0;
}

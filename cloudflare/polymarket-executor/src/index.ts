import {
  Chain,
  ClobClient,
  OrderType,
  Side,
} from "@polymarket/clob-client-v2";
import { createWalletClient, http } from "viem";
import { polygon } from "viem/chains";
import { privateKeyToAccount } from "viem/accounts";

const CLOB_HOST = "https://clob.polymarket.com";

type Env = Record<string, string | undefined>;

type ExecutionRequest = {
  market: Record<string, unknown>;
  decision: {
    action?: string;
    outcome?: string;
    price?: number;
    size?: number;
    amount_usd?: number;
    edge?: number;
    confidence?: number;
    reason?: string;
  };
  live?: boolean;
};

function asNumber(value: unknown, fallback = 0): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function normalizeTokenIds(value: unknown): string[] {
  if (Array.isArray(value)) return value.map(String);
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value);
      return Array.isArray(parsed) ? parsed.map(String) : [];
    } catch {
      return [];
    }
  }
  return [];
}

function findOutcomeIndex(market: Record<string, unknown>, outcome: string): number {
  const outcomes = Array.isArray(market.outcomes) ? market.outcomes.map(String) : [];
  const wanted = outcome.trim().toLowerCase();
  return outcomes.findIndex((item) => item.trim().toLowerCase() === wanted);
}

function isAuthorized(request: Request, token: string): boolean {
  return Boolean(token) && request.headers.get("authorization") === `Bearer ${token}`;
}

function riskCheck(decision: ExecutionRequest["decision"], limits: {
  minEdge: number;
  minConfidence: number;
  minPrice: number;
  maxPrice: number;
}) {
  const action = String(decision.action ?? "PASS").toUpperCase();
  if (!["BUY_YES", "BUY_NO", "PASS"].includes(action)) {
    return { ok: false, reason: "unsupported action" };
  }
  if (action === "PASS") return { ok: false, reason: "execution decision is PASS" };

  const edge = asNumber(decision.edge);
  const confidence = asNumber(decision.confidence);
  const price = asNumber(decision.price);
  if (edge < limits.minEdge) return { ok: false, reason: `edge ${edge.toFixed(4)} below minimum ${limits.minEdge.toFixed(4)}` };
  if (confidence < limits.minConfidence) return { ok: false, reason: `confidence ${confidence.toFixed(4)} below minimum ${limits.minConfidence.toFixed(4)}` };
  if (price < limits.minPrice || price > limits.maxPrice) {
    return { ok: false, reason: `entry price ${price.toFixed(4)} outside [${limits.minPrice.toFixed(4)}, ${limits.maxPrice.toFixed(4)}]` };
  }
  return { ok: true, reason: "risk checks passed" };
}

async function buildClient(runtime: Env) {
  const privateKey = runtime.POLYMARKET_PRIVATE_KEY;
  if (!privateKey) throw new Error("POLYMARKET_PRIVATE_KEY is not configured");

  const account = privateKeyToAccount(privateKey as `0x${string}`);
  const signer = createWalletClient({ account, chain: polygon, transport: http() });
  const signatureType = Number(runtime.POLYMARKET_SIGNATURE_TYPE ?? "0");
  const funderAddress = runtime.POLYMARKET_FUNDER_ADDRESS || runtime.POLYMARKET_WALLET_ADDRESS || account.address;
  const creds = runtime.POLYMARKET_API_KEY && runtime.POLYMARKET_API_SECRET && runtime.POLYMARKET_API_PASSPHRASE
    ? {
        key: runtime.POLYMARKET_API_KEY,
        secret: runtime.POLYMARKET_API_SECRET,
        passphrase: runtime.POLYMARKET_API_PASSPHRASE,
      }
    : undefined;

  if (signatureType === 3 && !creds) {
    throw new Error(
      "POLY_1271 deposit-wallet execution requires pre-issued CLOB credentials. Set POLYMARKET_API_KEY/POLYMARKET_API_SECRET/POLYMARKET_API_PASSPHRASE.",
    );
  }

  const base = new ClobClient({
    host: CLOB_HOST,
    chain: Chain.POLYGON,
    signer,
    ...(creds ? { creds } : {}),
    signatureType,
    funderAddress,
  });

  if (creds) return base;
  return new ClobClient({
    host: CLOB_HOST,
    chain: Chain.POLYGON,
    signer,
    creds: await base.createOrDeriveApiKey(),
    signatureType,
    funderAddress,
  });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (request.method !== "POST" || new URL(request.url).pathname !== "/execute") {
      return Response.json({ ok: true, service: "infotrader-polymarket-executor" });
    }

    if (!isAuthorized(request, env.POLYMARKET_EXECUTOR_TOKEN ?? "")) {
      return new Response("Unauthorized", { status: 401 });
    }

    let body: ExecutionRequest;
    try {
      body = await request.json() as ExecutionRequest;
    } catch {
      return Response.json({ executed: false, error: "invalid JSON body" }, { status: 400 });
    }

    const limits = {
      maxOrderUsd: asNumber(env.POLYMARKET_MAX_ORDER_USD, 10),
      minEdge: asNumber(env.POLYMARKET_MIN_EDGE, 0.05),
      minConfidence: asNumber(env.POLYMARKET_MIN_CONFIDENCE, 0.70),
      minPrice: asNumber(env.POLYMARKET_MIN_ENTRY_PRICE, 0.05),
      maxPrice: asNumber(env.POLYMARKET_MAX_ENTRY_PRICE, 0.95),
    };

    const checked = riskCheck(body.decision ?? {}, limits);
    if (!checked.ok) return Response.json({ executed: false, live: false, reason: checked.reason });

    const action = String(body.decision.action).toUpperCase();
    const outcome = String(body.decision.outcome ?? "").trim();
    const tokenIds = normalizeTokenIds(body.market.clob_token_ids ?? body.market.clobTokenIds);
    const fallbackOutcome = action === "BUY_YES" ? "Yes" : "No";
    const outcomeIndex = findOutcomeIndex(body.market, outcome || fallbackOutcome);
    const tokenId = outcomeIndex >= 0 ? tokenIds[outcomeIndex] : tokenIds[action === "BUY_NO" ? 1 : 0];

    if (!tokenId) {
      return Response.json({ executed: false, live: false, reason: "no matching CLOB token ID for selected outcome" }, { status: 400 });
    }

    const price = asNumber(body.decision.price);
    let size = asNumber(body.decision.size);
    const requestedUsd = asNumber(body.decision.amount_usd, limits.maxOrderUsd);
    if (size <= 0) size = Math.min(limits.maxOrderUsd, requestedUsd) / price;
    if (size * price > limits.maxOrderUsd) size = limits.maxOrderUsd / price;

    if (!body.live) {
      return Response.json({
        executed: false,
        simulated: true,
        live: false,
        action,
        outcome,
        token_id: tokenId,
        price,
        size: Number(size.toFixed(4)),
        amount_usd: Number((size * price).toFixed(2)),
        reason: "dry-run: live execution not requested",
      });
    }

    try {
      const client = await buildClient(env);
      const tickSize = String(body.market.tick_size ?? body.market.tickSize ?? await client.getTickSize(tokenId));
      const negRisk = Boolean(body.market.neg_risk ?? body.market.negRisk ?? await client.getNegRisk(tokenId));
      const response = await client.createAndPostOrder(
        { tokenID: tokenId, price, size, side: Side.BUY },
        { tickSize, negRisk },
        OrderType.GTC,
      );

      return Response.json({
        executed: true,
        live: true,
        action,
        outcome,
        token_id: tokenId,
        price,
        size: Number(size.toFixed(4)),
        amount_usd: Number((size * price).toFixed(2)),
        order: response,
      });
    } catch (error) {
      return Response.json({
        executed: false,
        live: false,
        action,
        outcome,
        price,
        size: Number(size.toFixed(4)),
        amount_usd: Number((size * price).toFixed(2)),
        reason: error instanceof Error ? error.message : String(error),
      }, { status: 502 });
    }
  },
};

from __future__ import annotations

import os

DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"


def place_order(token_id: str, side: str, price: float, size_usd: float, reason: str = "") -> dict:
    if not DRY_RUN:
        raise RuntimeError(
            "Live execution is intentionally disabled in this release. "
            "Polymarket's mainnet wallet/order signing flow is changing; "
            "wire and verify a current execution adapter before using real funds."
        )
    size_shares = round(size_usd / max(price, 0.01), 2)
    message = f"[DRY_RUN] Would {side.upper()} {size_shares} shares @ {price:.4f} (${size_usd:.2f}) token={token_id}. Reason: {reason}"
    print(message)
    return {"dry_run": True, "message": message, "token_id": token_id, "side": side}


def close_position(token_id: str, size_shares: float, price: float, reason: str = "") -> dict:
    if not DRY_RUN:
        raise RuntimeError("Live execution is intentionally disabled in this release; use a separately verified Polymarket execution adapter.")
    message = f"[DRY_RUN] Would CLOSE {size_shares} shares @ {price:.4f} token={token_id}. Reason: {reason}"
    print(message)
    return {"dry_run": True, "message": message, "token_id": token_id}

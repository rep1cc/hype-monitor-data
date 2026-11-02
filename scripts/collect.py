#!/usr/bin/env python3
"""Collects public Hyperliquid metrics for the HYPE monitor. Standard library only.

Writes data/latest.json and appends one row per UTC day to data/history.jsonl.
A metric that cannot be read is null with a reason in "errors"; it is never guessed.
Exit code: 0 if at least one block was read, 1 if all failed (nothing is overwritten).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = os.environ.get("HL_API_URL", "https://api.hyperliquid.xyz/info")
DATA = Path(os.environ.get("DATA_DIR", "data"))
AF_ADDRESS = "0xfefefefefefefefefefefefefefefefefefefefe"
HLP_ADDRESS = "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"
ATTEMPTS = 3


def post(body):
    payload = json.dumps(body).encode()
    last = None
    for attempt in range(ATTEMPTS):
        request = urllib.request.Request(
            API,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "hype-monitor/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last = error
            if attempt < ATTEMPTS - 1:
                time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"{body.get('type')}: {last}")


def hype_market():
    meta, ctxs = post({"type": "metaAndAssetCtxs"})
    names = [asset["name"] for asset in meta["universe"]]
    ctx = ctxs[names.index("HYPE")]
    mark = float(ctx["markPx"])
    open_interest = float(ctx["openInterest"])
    return {
        "mark_px": mark,
        "funding_hl_8h_pct": round(float(ctx["funding"]) * 8 * 100, 5),
        "hype_oi_hl_busd": round(open_interest * mark / 1e9, 3),
        "hype_vol24_musd": round(float(ctx["dayNtlVlm"]) / 1e6, 1),
    }


def assistance_fund():
    state = post({"type": "spotClearinghouseState", "user": AF_ADDRESS})
    for balance in state["balances"]:
        if balance["coin"] == "HYPE":
            return {"af_hype": float(balance["total"]), "af_hype_m": round(float(balance["total"]) / 1e6, 3)}
    raise RuntimeError("HYPE balance not found in Assistance Fund state")


def hlp():
    details = post({"type": "vaultDetails", "vaultAddress": HLP_ADDRESS})
    month = dict(details["portfolio"])["month"]
    tvl = float(month["accountValueHistory"][-1][1])
    pnl = float(month["pnlHistory"][-1][1])
    return {
        "hlp_tvl_musd": round(tvl / 1e6, 2),
        "hlp_pnl_30d_musd": round(pnl / 1e6, 2),
        "hlp_pnl_30d_pct": round(pnl / tvl * 100, 2) if tvl else None,
    }


def hip3():
    """Sums every builder-deployed dex. Any failed dex voids the total, to avoid an undercount."""
    dexes = [dex["name"] for dex in post({"type": "perpDexs"}) if dex and dex.get("name")]
    if not dexes:
        raise RuntimeError("perpDexs returned no HIP-3 dexes")
    by_dex = {}
    for name in dexes:
        meta, ctxs = post({"type": "metaAndAssetCtxs", "dex": name})
        total = 0.0
        for ctx in ctxs[: len(meta["universe"])]:
            price = ctx.get("markPx") or ctx.get("oraclePx")
            if price:
                total += float(ctx.get("openInterest") or 0) * float(price)
        by_dex[name] = round(total / 1e6, 2)
        time.sleep(0.2)
    return {"hip3_oi_busd": round(sum(by_dex.values()) / 1e3, 3), "hip3_oi_by_dex_musd": by_dex}


BLOCKS = {
    "hype_market": (hype_market, ["mark_px", "funding_hl_8h_pct", "hype_oi_hl_busd", "hype_vol24_musd"]),
    "assistance_fund": (assistance_fund, ["af_hype", "af_hype_m"]),
    "hlp": (hlp, ["hlp_tvl_musd", "hlp_pnl_30d_musd", "hlp_pnl_30d_pct"]),
    "hip3": (hip3, ["hip3_oi_busd", "hip3_oi_by_dex_musd"]),
}


def read_history(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def af_deltas(history, today, balance):
    """AF growth in HYPE against the newest stored row at least N days old."""
    result = {}
    for days in (1, 7, 30):
        cutoff = (today - timedelta(days=days)).isoformat()
        past = [r for r in history if r["date"] <= cutoff and r.get("af_hype") is not None]
        key = f"af_change_{days}d_hype"
        if balance is None or not past:
            result[key] = None
            result[f"{key}_from"] = None
        else:
            result[key] = round(balance - past[-1]["af_hype"], 2)
            result[f"{key}_from"] = past[-1]["date"]
    return result


def main():
    now = datetime.now(timezone.utc)
    row = {"date": now.date().isoformat(), "fetched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    errors = {}
    for name, (collect, fields) in BLOCKS.items():
        try:
            row.update(collect())
        except Exception as error:  # one failed block must not lose the others
            errors[name] = f"{type(error).__name__}: {error}"
            row.update({field: None for field in fields})

    if len(errors) == len(BLOCKS):
        print(json.dumps(errors, indent=2), file=sys.stderr)
        return 1

    DATA.mkdir(parents=True, exist_ok=True)
    history_path = DATA / "history.jsonl"
    stored = read_history(history_path)
    history = [r for r in stored if r["date"] != row["date"]]
    earlier_today = next((r for r in stored if r["date"] == row["date"]), {})
    row_for_history = {k: v for k, v in row.items() if k != "hip3_oi_by_dex_musd"}
    # A later partial run must not erase values already read today.
    history.append({**earlier_today, **{k: v for k, v in row_for_history.items() if v is not None}})
    history_path.write_text("".join(json.dumps(r) + "\n" for r in history))

    latest = {
        **row,
        **af_deltas(history[:-1], now.date(), row.get("af_hype")),
        "status": "partial" if errors else "ok",
        "errors": errors,
        "source": API,
    }
    (DATA / "latest.json").write_text(json.dumps(latest, indent=2) + "\n")
    print(json.dumps(latest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

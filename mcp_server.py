#!/usr/bin/env python3
"""
TASMAC MCP server. Thin stdio transport over tasmac_core.

All the logic lives in tasmac_core, which is stdlib-only and works fine as a
plain CLI. This module only exposes it as MCP tools so any client (Claude Code,
Claude Desktop, anything else that speaks MCP) can ask about liquor stock in a
Tamil Nadu government shop in plain language.

Run:  python3 mcp_server.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

import tasmac_core as core

INSTRUCTIONS = """
You can look up live liquor stock and MRP in any TASMAC shop in Tamil Nadu.
TASMAC is the Tamil Nadu state government retail monopoly for alcohol.

Start from tasmac_find_shop if the user does not know their shop number: it
takes an area, a district or a pincode. Then tasmac_stock returns that shop's
full catalogue with per-bottle MRP in rupees and current stock counts.

Notes that matter when answering:
- Categories are WINE, WHISKY, BRANDY, RUM, GIN, VODKA, BEER, LIQUOR. The
  origin field marks IFL (imported) or OTHER (out of state); blank is the
  standard TASMAC range. These prefixes are the site's own, not documented.
- TASMAC's own categorisation is loose. Soju, tequila and some flavoured
  drinks are filed under WINE. Say so rather than presenting them as wine.
- Stock is a count of bottles on hand, refreshed by TASMAC roughly daily, so
  treat it as this morning's picture rather than a live till feed.
- Shops do not temperature control their stock. That is worth mentioning for
  white wine, sparkling wine and anything delicate.
- TASMAC's district and taluka tags are wrong for a slice of its shops, so a
  district listing can contain a shop hundreds of km away. Those are marked
  (?) in the output. The street address is the reliable field, not the tag.
- Pincode and area searches are resolved through OpenStreetMap, so the shop
  distances are as-the-crow-flies from that point, not driving distance.
- Every lookup saves a dated local snapshot, so tasmac_changes and
  tasmac_history get more useful the longer the tools are used.
"""

mcp = FastMCP("tasmac", instructions=INSTRUCTIONS)

_READONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
# tasmac_stock hits the network and appends a local snapshot, so it is not
# strictly read-only. It changes nothing outside its own cache file, and setting
# TASMAC_NO_HISTORY=1 makes it read-only in the strict sense.
_LOOKUP = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)


@mcp.tool(annotations=_LOOKUP)
def tasmac_stock(shop_number: str, category: str = "", query: str = "",
                 max_price: int = 0, min_price: int = 0,
                 include_out_of_stock: bool = False, sort: str = "price",
                 limit: int = 60) -> str:
    """Look up what a TASMAC shop currently has in stock, with MRP per bottle.

    Args:
        shop_number: The TASMAC shop number, e.g. "4107".
        category: Optional filter. One of WINE, WHISKY, BRANDY, RUM, GIN,
            VODKA, BEER, LIQUOR. Empty means every category.
        query: Optional substring match on the product name, e.g. "sula".
        max_price: Optional ceiling on MRP per bottle in rupees. 0 means none.
        min_price: Optional floor on MRP per bottle in rupees. 0 means none.
        include_out_of_stock: Include products the shop carries but has none of.
        sort: price, price_desc, stock or name.
        limit: Maximum rows to return.
    """
    try:
        data = core.fetch_shop(shop_number)
    except (LookupError, RuntimeError) as e:
        return str(e)
    items = core.filter_items(
        data["items"], category=category or None, in_stock_only=not include_out_of_stock,
        max_price=max_price or None, min_price=min_price or None,
        query=query or None, sort=sort)
    return core.format_stock(data, items, limit)


@mcp.tool(annotations=_LOOKUP)
def tasmac_find_shop(area: str = "", district: str = "", pincode: str = "",
                     limit: int = 10) -> str:
    """Find TASMAC shop numbers by area, district or pincode. Use this first
    when the user does not know their shop number.

    Area and pincode return shops ordered by distance. A district returns every
    shop it has, grouped by taluka.

    Args:
        area: Area, locality or landmark, e.g. "Sholinganallur" or "Anna Nagar".
        district: Revenue district name, e.g. "Chennai" or "Coimbatore".
        pincode: Six digit pincode, e.g. "600119".
        limit: Maximum shops to return.
    """
    try:
        return core.format_shops(
            core.find_shops(area=area, district=district, pincode=pincode, limit=limit), limit)
    except (LookupError, RuntimeError) as e:
        return str(e)


@mcp.tool(annotations=_READONLY)
def tasmac_changes(shop_number: str, category: str = "", since: str = "") -> str:
    """Show what changed at a shop between two saved snapshots: new arrivals,
    sold out lines, price changes and stock movements.

    Requires at least two lookups on different days for that shop.

    Args:
        shop_number: The TASMAC shop number, e.g. "4107".
        category: Optional category filter, e.g. WINE.
        since: Optional YYYY-MM-DD snapshot to compare against. Defaults to the
            snapshot immediately before the newest one.
    """
    return core.format_changes(core.changes(shop_number, category or None, since or None))


@mcp.tool(annotations=_READONLY)
def tasmac_history(shop_number: str, product: str) -> str:
    """Show price and stock over time for products matching a name.

    Args:
        shop_number: The TASMAC shop number, e.g. "4107".
        product: Substring of the product name, e.g. "vina sol".
    """
    return core.format_history(core.history(shop_number, product))


@mcp.tool(annotations=_READONLY)
def tasmac_snapshots(shop_number: str) -> str:
    """List the dates on which this shop's stock was captured locally.

    Args:
        shop_number: The TASMAC shop number, e.g. "4107".
    """
    dates = core.snapshot_dates(shop_number)
    if not dates:
        return f"No snapshots yet for shop {shop_number}. Run tasmac_stock once to start the history."
    return f"Shop #{shop_number}: {len(dates)} snapshots, {dates[-1]} to {dates[0]}\n" + "\n".join(dates)


if __name__ == "__main__":
    mcp.run(transport="stdio")

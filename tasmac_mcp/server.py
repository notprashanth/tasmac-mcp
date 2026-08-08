#!/usr/bin/env python3
"""
TASMAC MCP server. Thin stdio transport over tasmac_core.

All the logic lives in tasmac_core, which is stdlib-only and works fine as a
plain CLI. This module only exposes it as MCP tools so any client (Claude Code,
Claude Desktop, anything else that speaks MCP) can ask about liquor stock in a
Tamil Nadu government shop in plain language.

Run:  tasmac-mcp          (installed)
      python3 mcp_server.py   (from a checkout)
"""
import json
import warnings

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import Icon, ToolAnnotations
from pydantic import ValidationError

from . import core
from . import __version__

INSTRUCTIONS = """
You can look up live liquor stock and MRP in any TASMAC shop in Tamil Nadu.
TASMAC is the Tamil Nadu state government retail monopoly for alcohol.

There are two directions to search:
- "what does shop X have" -> tasmac_stock. If the user does not know their
  shop number, tasmac_find_shop takes an area, a district or a pincode.
- "where can I get bottle Y near me" -> tasmac_find_product.

Stock figures are per bottle counts and MRP is in rupees.

Every tool returns a compact table by default. Pass format="json" for the full
structured result when you need to compute over the rows rather than report
them: the table truncates long names and addresses and drops several fields.

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

# The icon travels inside the server as a data URI so it works over stdio with
# no host to fetch from. Source lives in icon.svg at the repo root; the two are
# kept in step by a test. A bottle neck over a map pin: what it is, and what it
# tells you.
ICON_SRC = (
    "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdm"
    "ciIHZpZXdCb3g9IjAgMCA2NCA2NCIgd2lkdGg9IjY0IiBoZWlnaHQ9IjY0IiByb2xlPSJpbWciIG"
    "FyaWEtbGFiZWw9IlRBU01BQyBzdG9jayI+CiAgPHJlY3Qgd2lkdGg9IjY0IiBoZWlnaHQ9IjY0Ii"
    "ByeD0iMTQiIGZpbGw9IiMxMzFBMzMiLz4KICA8ZyBmaWxsPSIjRTJBMjRFIj4KICAgIDwhLS0gY2"
    "FwIGFuZCBuZWNrLCBvdmVybGFwcGluZyB0aGUgYm9keSBzbyB0aGUgc2lsaG91ZXR0ZSBpcyBvbm"
    "Ugc2hhcGUgLS0+CiAgICA8cmVjdCB4PSIyNi41IiB5PSI1LjUiIHdpZHRoPSIxMSIgaGVpZ2h0PS"
    "I1IiByeD0iMiIvPgogICAgPHBhdGggZD0iTTI4IDkuNSBoOCB2OSBjMCAyLjUgLTggMi41IC04ID"
    "AgeiIvPgogICAgPCEtLSBib2R5OiBhIGxvY2F0aW9uIHBpbiwgc28gdGhlIG1hcmsgc2F5cyBib3"
    "R0bGUgb24gdG9wIGFuZCBwbGFjZSBiZWxvdyAtLT4KICAgIDxwYXRoIGQ9Ik0zMiA1OQogICAgIC"
    "AgICAgICAgQyAzMiA1OSA0Ny41IDQxLjUgNDcuNSAzNAogICAgICAgICAgICAgQSAxNS41IDE1Lj"
    "UgMCAxIDAgMTYuNSAzNAogICAgICAgICAgICAgQyAxNi41IDQxLjUgMzIgNTkgMzIgNTkgWiIvPg"
    "ogIDwvZz4KICA8Y2lyY2xlIGN4PSIzMiIgY3k9IjMzLjUiIHI9IjUuNiIgZmlsbD0iIzEzMUEzMy"
    "IvPgo8L3N2Zz4K"
)
ICON = Icon(src=ICON_SRC, mimeType="image/svg+xml", sizes=["any"])

# pydantic_settings warns that FastMCP's own Settings model leaves 'lifespan'
# as an unresolved forward reference, the moment the server is constructed. It
# is upstream, it changes nothing here, and it is the first thing anyone sees in
# their MCP log, which makes a working server look broken. Scoped to the one
# construction rather than filtered globally, so nothing else is silenced.
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore", message=r"Field 'lifespan' has an incomplete definition")
    mcp = FastMCP(
        "tasmac",
        instructions=INSTRUCTIONS,
        website_url="https://github.com/notprashanth/tasmac-mcp",
        icons=[ICON],
    )

# FastMCP does not pass a version through to the underlying server, so clients
# are told the MCP SDK's version instead of ours. Left alone, a client shows
# "tasmac 1.27.0", which is a fact about the SDK and a lie about this package.
mcp._mcp_server.version = __version__

_READONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
# tasmac_stock hits the network and appends a local snapshot, so it is not
# strictly read-only. It changes nothing outside its own cache file, and setting
# TASMAC_NO_HISTORY=1 makes it read-only in the strict sense.
_LOOKUP = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)


def _out(payload, text: str, fmt: str) -> str:
    """Return either the formatted table or the raw structure behind it."""
    if (fmt or "text").strip().lower() == "json":
        return json.dumps(payload, indent=2, default=str)
    return text


@mcp.tool(annotations=_LOOKUP)
def tasmac_stock(shop_number: str, category: str = "", query: str = "",
                 max_price: int = 0, min_price: int = 0,
                 include_out_of_stock: bool = False, sort: str = "price",
                 limit: int = 60, format: str = "text") -> str:
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
        format: 'text' (default) for a compact table, or 'json' for the full
            structured result. The table truncates long names and addresses and
            omits fields such as product_id, pack_size, supplier and
            coordinates. Use json when you need to compute over the rows rather
            than report them; every tool here takes the same argument.
    """
    try:
        data = core.fetch_shop(shop_number)
    except (LookupError, RuntimeError) as e:
        return _out({"error": str(e)}, str(e), format)
    items = core.filter_items(
        data["items"], category=category or None, in_stock_only=not include_out_of_stock,
        max_price=max_price or None, min_price=min_price or None,
        query=query or None, sort=sort)
    return _out({**data, "items": items[:limit]},
                core.format_stock(data, items, limit), format)


@mcp.tool(annotations=_LOOKUP)
def tasmac_find_shop(area: str = "", district: str = "", pincode: str = "",
                     limit: int = 10, format: str = "text") -> str:
    """Find TASMAC shop numbers by area, district or pincode. Use this first
    when the user does not know their shop number.

    Area and pincode return shops ordered by distance. A district returns every
    shop it has, grouped by taluka.

    Args:
        area: Area, locality or landmark, e.g. "Sholinganallur" or "Anna Nagar".
        district: Revenue district name, e.g. "Chennai" or "Coimbatore".
        pincode: Six digit pincode, e.g. "600119".
        limit: Maximum shops to return.
        format: text or json. See the note on tasmac_stock.
    """
    try:
        res = core.find_shops(area=area, district=district, pincode=pincode, limit=limit)
        return _out(res, core.format_shops(res, limit), format)
    except (LookupError, RuntimeError) as e:
        return _out({"error": str(e)}, str(e), format)


@mcp.tool(annotations=_LOOKUP)
def tasmac_find_product(product: str, area: str = "", pincode: str = "",
                        category: str = "", limit: int = 10,
                        format: str = "text") -> str:
    """Find which shops near a place currently stock a particular bottle.

    Use this for "where can I get X near me". It is the reverse of
    tasmac_stock, which asks what one shop has.

    Args:
        product: Product or brand name, e.g. "Sula Brut" or "Old Monk". Fewer
            words match better. The catalogue is statewide, so a match here
            does not mean it is on a shelf nearby.
        area: Area, locality or landmark to search around.
        pincode: Six digit pincode to search around. Give area or pincode.
        category: Optional filter, e.g. WINE, to disambiguate a shared name.
        limit: Maximum shops to return.
        format: text or json. json also exposes every catalogue variant that
            matched, including the ones not searched.
    """
    try:
        res = core.find_product(product, area=area, pincode=pincode,
                                category=category, limit=limit)
        return _out(res, core.format_product_search(res, limit), format)
    except (LookupError, RuntimeError) as e:
        return _out({"error": str(e)}, str(e), format)


@mcp.tool(annotations=_READONLY)
def tasmac_changes(shop_number: str, category: str = "", since: str = "",
                   format: str = "text") -> str:
    """Show what changed at a shop between two saved snapshots: new arrivals,
    sold out lines, price changes and stock movements.

    Requires at least two lookups on different days for that shop.

    Args:
        shop_number: The TASMAC shop number, e.g. "4107".
        category: Optional category filter, e.g. WINE.
        since: Optional YYYY-MM-DD snapshot to compare against. Defaults to the
            snapshot immediately before the newest one.
        format: text or json. json returns the appeared, vanished, repriced and
            movers lists in full, unbounded by the display limit.
    """
    if not core.WRITE_HISTORY:
        msg = ("Change tracking needs a local install: it compares snapshots "
               "this machine took on different days. A shared server keeps no "
               "such archive. github.com/notprashanth/tasmac-mcp")
        return _out({"error": msg}, msg, format)
    res = core.changes(shop_number, category or None, since or None)
    return _out(res, core.format_changes(res), format)


@mcp.tool(annotations=_READONLY)
def tasmac_history(shop_number: str, product: str, format: str = "text") -> str:
    """Show price and stock over time for products matching a name.

    Args:
        shop_number: The TASMAC shop number, e.g. "4107".
        product: Substring of the product name, e.g. "vina sol".
        format: text or json.
    """
    if not core.WRITE_HISTORY:
        msg = ("History is only kept by a local install, where the archive is "
               "yours. This shared server does not record one. Run it yourself "
               "to get history: github.com/notprashanth/tasmac-mcp")
        return _out({"error": msg}, msg, format)
    rows = core.history(shop_number, product)
    return _out(rows, core.format_history(rows), format)


@mcp.tool(annotations=_READONLY)
def tasmac_snapshots(shop_number: str, format: str = "text") -> str:
    """List the dates on which this shop's stock was captured locally.

    Args:
        shop_number: The TASMAC shop number, e.g. "4107".
        format: text or json.
    """
    dates = core.snapshot_dates(shop_number)
    if not dates:
        return _out({"shop": shop_number, "snapshots": []},
                    f"No snapshots yet for shop {shop_number}. "
                    "Run tasmac_stock once to start the history.", format)
    return _out(
        {"shop": shop_number, "count": len(dates), "snapshots": dates},
        f"Shop #{shop_number}: {len(dates)} snapshots, {dates[-1]} to {dates[0]}\n"
        + "\n".join(dates), format)


# A caller that forgets a required argument gets pydantic's own report:
# "Error executing tool tasmac_stock: 1 validation error for tasmac_stockArguments
# shop_number Field required [type=missing, input_value=...]" and a link to
# pydantic's docs. All true, and useless to the model that has to recover from
# it. tasmac_find_shop already answers a bare call with a sentence telling you
# what to give it; the rest should sound the same.
#
# Keyed by (tool, argument) where the same name means different things, by
# argument alone otherwise. Each entry reads as a noun phrase after "needs X:".
_ARG_HELP = {
    "shop_number": ('the TASMAC shop number, such as "4107". If you do not know '
                    "it, tasmac_find_shop takes an area, a district or a pincode."),
    ("tasmac_find_product", "product"): ('the product or brand name to look for, '
                                         'such as "Old Monk". Fewer words match better.'),
    ("tasmac_history", "product"): 'part of the product name, such as "vina sol".',
}


def _explain(tool: str, exc: ValidationError) -> str:
    """Turn a pydantic ValidationError into something a caller can act on."""
    lines = []
    for err in exc.errors():
        field = ".".join(str(p) for p in err.get("loc") or ()) or "an argument"
        if err.get("type") == "missing":
            hint = _ARG_HELP.get((tool, field)) or _ARG_HELP.get(field)
            lines.append(f"{tool} needs {field}: {hint}" if hint
                         else f"{tool} needs {field}.")
        else:
            lines.append(f"{tool} could not use {field}. {err.get('msg', 'It is not valid')}.")
    return " ".join(lines) or f"{tool} was called with arguments it could not use."


_fastmcp_call_tool = mcp.call_tool


async def _call_tool(name: str, arguments: dict):
    """Same handler FastMCP installs, with a readable message on bad arguments.

    Raising keeps the result flagged as an error, which is what the caller needs
    to see. Only the wording changes.
    """
    try:
        return await _fastmcp_call_tool(name, arguments)
    except ToolError as e:
        if isinstance(e.__cause__, ValidationError):
            raise ToolError(_explain(name, e.__cause__)) from None
        raise


# Re-registering replaces FastMCP's handler for CallToolRequest. validate_input
# stays off to match what _setup_handlers does: FastMCP converts arguments
# before validating them, and the lowlevel schema check would pre-empt that.
mcp._mcp_server.call_tool(validate_input=False)(_call_tool)


def main() -> None:
    """stdio: one server per person, on their own machine."""
    mcp.run(transport="stdio")


def main_http() -> None:
    """HTTP: one server, many people. A different thing, so it behaves differently.

    Snapshot history is switched off here. Locally the archive is yours and
    "what changed since yesterday" means something. Shared by strangers it
    would be one global archive presented as personal, which is worse than not
    offering it, so tasmac_changes and tasmac_history say so plainly instead.

    PORT is read from the environment because every container host sets it.
    """
    import os

    core.WRITE_HISTORY = False
    mcp.settings.host = os.environ.get("HOST", "0.0.0.0")
    mcp.settings.port = int(os.environ.get("PORT", "8080"))
    mcp.settings.streamable_http_path = os.environ.get("MCP_PATH", "/mcp")
    # Stateless suits a public endpoint: no per-connection state to leak or
    # exhaust, and it survives a host that load balances across instances.
    mcp.settings.stateless_http = True
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()

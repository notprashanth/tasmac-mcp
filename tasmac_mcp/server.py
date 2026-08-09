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
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Icon, ToolAnnotations
from pydantic import ValidationError

from . import core
from . import __version__

INSTRUCTIONS = """
You can look up live liquor stock and MRP in any TASMAC shop in Tamil Nadu.
TASMAC is the Tamil Nadu state government retail monopoly for alcohol.

"What should I buy" is tasmac_recommend, which ranks by value against duty
free or by rarity. Two more directions to search:
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
- A stock number says nothing about how fast a bottle moves. One observation
  is restock size minus sales since restock, and those two are not separable
  from a single reading, so 44 bottles is not evidence of brisk turnover and
  51 is not evidence of a dud. Do not infer popularity, freshness or restock
  cadence from it. In particular, "18 in stock" is not a promise that 18 are
  there when someone arrives, and it is not a basis for a bulk order: say so
  when a user is planning to buy in quantity or drive any distance.
- Rarity, where given, counts surveyed shops only. A bottle carried by 2 of
  157 is genuinely hard to find; a bottle absent from the survey may still sit
  in a shop nobody opened.
- Shops do not temperature control their stock. That is worth mentioning for
  white wine, sparkling wine and anything delicate.
- "elite" is a licence class, not a measure of what a shop stocks. Surveyed
  statewide, 63 of 157 elite shops carry nothing above Rs 3,000, so sending
  someone to the nearest elite shop often sends them to two fortified wines.
  Use the computed `tier` instead (flagship, premium, standard, basic), and
  when the two disagree say so: "licensed elite, stocks nothing premium" is
  the single most useful thing you can tell someone. A shop with no tier was
  never surveyed, which is not the same as basic.
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


# Internal plumbing nobody calling this tool can act on. Counting elite shops
# in Chennai meant pulling 375 rows of lat/lon, taluka_id, district_id,
# "km": null and "misfiled": false to read one boolean.
_NOISE_KEYS = ("taluka_id", "district_id")


def _trim(value):
    """Drop nulls, internal ids and false flags from JSON output."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in _NOISE_KEYS or v is None:
                continue
            if k == "misfiled" and not v:          # only worth saying when true
                continue
            out[k] = _trim(v)
        return out
    if isinstance(value, list):
        return [_trim(v) for v in value]
    return value


def _out(payload, text: str, fmt: str) -> str:
    """Return either the formatted table or the raw structure behind it."""
    if (fmt or "text").strip().lower() == "json":
        return json.dumps(_trim(payload), indent=2, default=str)
    return text


@mcp.tool(annotations=_LOOKUP)
def tasmac_stock(shop_number: str, category: str = "", query: str = "",
                 max_price: int = 0, min_price: int = 0,
                 include_out_of_stock: bool = False, sort: str = "price",
                 limit: int = 60, count_only: bool = False,
                 format: str = "text") -> str:
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
        count_only: Return just the counts, no rows. Use when the question is
            "how many" rather than "which", so counting never costs a full
            payload.
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
    if count_only:
        in_stock = sum(1 for i in data["items"] if i["stock"] > 0)
        summary = {"shop": data["shop"], "district": data["district"],
                   "matching": len(items), "in_stock": in_stock,
                   "listed": len(data["items"])}
        return _out(summary,
                    f"Shop #{data['shop']}: {len(items)} matching, "
                    f"{in_stock} in stock of {len(data['items'])} listed.", format)
    return _out({**data, "items": items[:limit]},
                core.format_stock(data, items, limit), format)


@mcp.tool(annotations=_LOOKUP)
def tasmac_find_shop(area: str = "", district: str = "", pincode: str = "",
                     tier: str = "", limit: int = 10, count_only: bool = False,
                     format: str = "text") -> str:
    """Find TASMAC shop numbers by area, district or pincode. Use this first
    when the user does not know their shop number.

    Area and pincode return shops ordered by distance. A district returns every
    shop it has, grouped by taluka.

    Args:
        area: Area, locality or landmark, e.g. "Sholinganallur" or "Anna Nagar".
        district: Revenue district name, e.g. "Chennai" or "Coimbatore".
        pincode: Six digit pincode, e.g. "600119".
        tier: Optional floor on what the shop actually stocks, computed from a
            survey rather than from TASMAC's licence class: flagship, premium,
            standard or basic. "premium" means 40 or more lines above Rs 3,000.
            Prefer this over the elite tag when someone wants good bottles:
            63 of 157 elite shops statewide stock nothing above Rs 3,000.
            Shops that were never surveyed are excluded by this filter, since
            absence from the survey means unknown rather than basic.
        limit: Maximum shops to return.
        count_only: Return counts only, no rows. Answers "how many shops in
            Chennai" without paying for 375 of them.
        format: text or json. See the note on tasmac_stock.
    """
    try:
        res = core.find_shops(area=area, district=district, pincode=pincode,
                              tier=tier, limit=limit)
        if count_only:
            shops = res.get("shops", [])
            summary = {"label": res.get("label"), "shops": len(shops),
                       "by_tier": {t: sum(1 for s in shops if s.get("tier") == t)
                                   for t in core.TIER_ORDER
                                   if any(s.get("tier") == t for s in shops)},
                       "elite": sum(1 for s in shops if s.get("elite")),
                       "misfiled": sum(1 for s in shops if s.get("misfiled"))}
            line = (f"{summary['shops']} shops, {summary['label']}. "
                    f"{summary['elite']} tagged elite")
            if summary["misfiled"]:
                line += f", {summary['misfiled']} filed in the wrong district"
            return _out(summary, line + ".", format)
        return _out(res, core.format_shops(res, limit), format)
    except (LookupError, RuntimeError) as e:
        return _out({"error": str(e)}, str(e), format)


@mcp.tool(annotations=_LOOKUP)
def tasmac_find_product(product: str, area: str = "", pincode: str = "",
                        category: str = "", limit: int = 10,
                        count_only: bool = False, format: str = "text") -> str:
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
        count_only: Return how many shops stock it, without the rows.
        format: text or json. json also exposes every catalogue variant that
            matched, including the ones not searched.
    """
    try:
        res = core.find_product(product, area=area, pincode=pincode,
                                category=category, limit=limit)
        if count_only:
            shops = res.get("shops", [])
            summary = {"query": product, "near": res.get("near"),
                       "shops_stocking": len(shops),
                       "total_bottles": sum(s["stock"] for s in shops)}
            return _out(summary,
                        f"'{product}' near {res.get('near')}: stocked at "
                        f"{len(shops)} shop(s), {summary['total_bottles']} bottles.",
                        format)
        return _out(res, core.format_product_search(res, limit), format)
    except (LookupError, RuntimeError) as e:
        return _out({"error": str(e)}, str(e), format)


@mcp.tool(annotations=_LOOKUP)
def tasmac_recommend(prefer: str = "value", category: str = "", max_price: int = 0,
                     min_price: int = 0, area: str = "", pincode: str = "",
                     limit: int = 5, format: str = "text") -> str:
    """Suggest what to actually buy, ranked by value or rarity rather than price.

    Use this for "what is worth buying", "best value whisky near me", or
    "something I cannot get elsewhere". For "what does this shop have" use
    tasmac_stock, and for "where is this bottle" use tasmac_find_product.

    Args:
        prefer: "value" ranks by price against Indian duty free, so a fair
            price rather than a cheap one. "rare" ranks by how few surveyed
            shops carry it.
        category: WINE, WHISKY, BRANDY, RUM, GIN, VODKA, BEER, LIQUOR,
            TEQUILA, SOJU.
        max_price: Budget ceiling in rupees. 0 means none.
        min_price: Floor in rupees. 0 means none.
        area: Area or landmark, so the answer says which shop to go to.
        pincode: Six digit pincode, same purpose.
        limit: How many bottles to suggest.
        format: text or json.

    Only 40 bottles carry a duty-free reference and all are above Rs 3,000, so
    prefer="value" ranks a small set on purpose. There is no taste, grape or
    peat metadata in the catalogue, so style requests ("something smoky") cannot
    be answered from this data: say so rather than inferring from the label.
    """
    try:
        res = core.recommend(prefer=prefer, category=category, max_price=max_price,
                             min_price=min_price, area=area, pincode=pincode, limit=limit)
        return _out(res, core.format_recommend(res, limit), format)
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
    unavailable = core.history_status()
    if unavailable:
        return _out({"error": unavailable}, unavailable, format)
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
    unavailable = core.history_status()
    if unavailable:
        return _out({"error": unavailable}, unavailable, format)
    rows = core.history(shop_number, product)
    return _out(rows, core.format_history(rows), format)


@mcp.tool(annotations=_READONLY)
def tasmac_snapshots(shop_number: str, format: str = "text") -> str:
    """List the dates on which this shop's stock was captured locally.

    Args:
        shop_number: The TASMAC shop number, e.g. "4107".
        format: text or json.
    """
    unavailable = core.history_status()
    if unavailable:
        return _out({"error": unavailable}, unavailable, format)
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


def _csv(value: str) -> list:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _transport_security(hosts: str, origins: str) -> TransportSecuritySettings:
    """Decide DNS-rebinding protection for a hosted instance.

    FastMCP turns this protection on by itself, but only ever for localhost: the
    constructor sees the default host and allows 127.0.0.1, localhost and [::1].
    main_http then moves the server to 0.0.0.0 and the stale allow-list stays
    behind, so a deployed instance rejects its own hostname with HTTP 421 and
    every browser client with HTTP 403 "Invalid Origin header". A client sending
    no Origin passes both checks, so curl reports a healthy server while
    Claude's connector, which sends Origin: https://claude.ai, spins forever.

    The SDK has no wildcard entry. Validation is an exact match plus a "host:*"
    port pattern, so "*" cannot be expressed as an allow-list entry at all, only
    as the protection switched off. A single "*" on either list already lets any
    value through that pair, so treat it as off rather than pretending the other
    half still guards something.

    Off is the right default here. This protection exists to stop a web page
    reaching a server bound to the user's own loopback. A public Cloud Run URL
    serving read-only lookups over public data is reachable by anyone already,
    and hosted mode registers nothing that writes.
    """
    allowed_hosts, allowed_origins = _csv(hosts), _csv(origins)
    if not allowed_hosts or "*" in allowed_hosts or "*" in allowed_origins:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


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
    # Sessions stay ON, and that is a correction rather than a preference.
    #
    # Stateless looked right for a public endpoint: nothing per-connection to
    # leak or exhaust, and it survives a host that load balances across
    # instances. But a stateless server issues no Mcp-Session-Id header, and
    # claude.ai's connector needs that to make any request after initialize.
    # The failure is silent in the worst way: initialize answers 200, so the
    # logs look healthy, and the connector then retries and reports only that
    # it "couldn't reach" the server. A stateless GET on the endpoint also
    # hangs open rather than answering, which is the second half of the same
    # problem. The Python SDK client tolerates both, so it is no guide here.
    #
    # Sessions do mean an instance holds state, so the deployment pins itself
    # to one instance. At this traffic that costs nothing.
    mcp.settings.stateless_http = os.environ.get("TASMAC_STATELESS", "") == "1"
    mcp.settings.transport_security = _transport_security(
        os.environ.get("TASMAC_ALLOWED_HOSTS", "*"),
        os.environ.get("TASMAC_ALLOWED_ORIGINS", "*"),
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()

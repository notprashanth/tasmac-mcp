# TASMAC Stock

Live liquor stock and MRP for any TASMAC shop in Tamil Nadu, without a browser.
Command line tool, plus an MCP server so you can just ask.

```
$ tasmac --find 600119
6 shops, 600119, Sholinganallur, Chennai (nearest first)

SHOP  KM       ADDRESS                                                   TYPE
----  -------  --------------------------------------------------------  -----
4107  0.98 km  Old No 51/ New No 16 Rajeev Gandhi Salai, Sholinganallur  elite

$ tasmac 4107 -c wine --max-price 2300
1340  Sula Vineyards Sauvignon Blanc       750ml  35   OTHER
2220  Vina Sol                             750ml  24   IFL
2280  Campo Viejo Tempranillo              750ml  13   IFL
```

Not affiliated with TASMAC or the Government of Tamil Nadu. It reads the same
public endpoints the official site reads, and nothing else.

`tasmac.tn.gov.in` is a thin client over a public JSON API. There is no auth, no
session, and no age gate on the API itself, so the entire browser flow (age gate,
location prompt, district picker, shop search, stock modal) collapses into one
HTTP POST. A full shop catalogue is about 550KB and comes back in under a second.

## The API

Base: `https://dashboard-api.tasmace2e.in/api`

Stock lives under `liquor/`, shop locations under `rv-shop/`.

| Endpoint | Body | Returns |
|---|---|---|
| `liquor/get-stockDetailsBy-shopNumber` | `{"i_ShopNumber": "4107"}` | One shop, full catalogue, **with `mrpPerBottle`** |
| `liquor/get-stockDetailsBy-DistrictId` | `{"p_districtId": 3}` | Every shop in a district, no MRP |
| `rv-shop/get-districtList` | `{}` | 38 districts with ids |
| `rv-shop/get-talukList` | `{}` | All taluks, with their district id |
| `rv-shop/get-shopList` | `{}` | All 4852 shop numbers, no address |
| `rv-shop/get-shopListBy-ShopNumber` | `{"i_ShopNumber": "4107"}` | Address and coordinates for one shop |
| `rv-shop/get-shopListBy-TalukId` | `{"i_TalukaId": 27}` | Every shop in a taluka, with addresses |
| `rv-shop/get-Nearby-ShopDetails` | `{"p_latitude": "12.90", "p_longitude": "80.22"}` | 451 shops sorted by `km` from that point |
| `liquor/get-productList` | `{}` | Statewide catalogue, 2126 variants, with MRP |
| `liquor/get-brandList` | `{}` | The 7 top level categories |
| `liquor/get-stockDetailsBy-ProductId/lat-long` | `{"p_latitude": "12.90", "p_longitude": "80.22", "p_productId": "1371"}` | Shops near that point stocking that product, by `km` |

The parameter names are the only hard part, and they are inconsistent across
the very same API. Stock wants `i_ShopNumber` and 409s on `shopNumber`,
`p_shopNumber` and `p_shop_number`. District stock wants `p_districtId`, not
`districtId`. Taluka wants `i_TalukaId`, not `i_TalukId` (which is the spelling
in the site's own service method name). Nearby wants `p_latitude` and
`p_longitude` **as strings**, and 409s on numbers. The 409 body helpfully lists
the required names, so probing is quick.

Other endpoints seen in the bundle but not used here:
`liquor/get-productListByBrand`, `liquor/get-stockDetails`,
`liquor/get-stockDetailsBy-DistrictId-talukId`,
`supplier/get-supplierListProductionType`.

`liquor/get-fl11-stockDetailsBy-ProductId/lat-long` is referenced once in the
site's JavaScript, is called by no UI, and **404s on every casing and path
variant** while the non-fl11 call beside it returns 200. It is dead frontend
code for a service that was never deployed, so there is nothing to build
against and no way to know what it would have returned.

FL-11, for the record, is not a bar licence. Under the Tamil Nadu Liquor
(Licence and Permit) Rules, 1981 it is the licence permitting TASMAC itself to
sell foreign liquor by retail, which TASMAC alone may apply for. It is the
licence behind the shops this tool already queries. Bars attached to TASMAC
shops fall under the Tamil Nadu Liquor Retail Vending (in Shops and Bars)
Rules, 2003 and are licensed separately.

`rv-shop/get-shopListBy-DistrictId` is broken: its validator demands four
parameters and its stored procedure accepts one, so it always fails. Listing a
district means walking its taluks, which is what the finder does.

Per-item fields: `productId`, `productName`, `brandName`, `unitName`,
`packSize`, `currentStock`, `mrpPerBottle`, `supplierName`, `BuyBackamt`.

### Quirks

- `brandName` is prefixed: `WINE`, `IFL-WINE` and `OTHER-WINE` are all wine.
  IFL appears to mean imported and OTHER out of state. Undocumented, so treat
  as a guess. The core splits this into `category` plus `origin`.
- TASMAC's categorisation is loose. Soju, a tequila and several flavoured
  drinks sit under WINE. Do not present them as wine without saying so.
- A shop's catalogue lists everything it is authorised to carry, most of it at
  `currentStock: 0`. Shop 4107 lists 2158 SKUs and stocks 261 of them.
- Stock refreshes roughly daily (the site shows its own "stock updated as on"
  timestamp), so this is a morning picture, not a live till feed.
- **District and taluka tags are wrong for a slice of the shops.** Shop 57 has a
  Chennai address and Chennai coordinates but is filed under Ariyalur district
  and Andimadam taluka, and its `revenue_district_id` is null in the master
  list. The address and coordinates agree with each other, so treat those as
  the truth and the administrative tag as suspect. The finder flags shops that
  sit more than 150km from the median of their own group with `(?)`.
- `isMallShop: 1` is what the site labels an Elite shop.
- There is no pincode anywhere in the API.
- **Products come back duplicated.** On 2026-08-08 the shop endpoint returned
  2,419 rows for 2,158 distinct products: every in-stock line appeared twice,
  with the copies identical. Left alone that doubles every count. The core
  collapses on `productId`.
- **The shop endpoint degrades badly.** Under a second one morning, 33 seconds
  the same afternoon, an outright 504 by evening, while lighter endpoints
  stayed fast throughout. Calls retry gateway failures (429, 5xx and network
  errors) with a 2s then 5s backoff, but never retry a 409, which means the
  parameter contract changed and waiting will not help. `TASMAC_TIMEOUT`
  (default 90s) caps one attempt; `TASMAC_DEADLINE` (default 45s) stops new
  attempts starting. The deadline is shorter than the timeout on purpose: a
  fast failure gets all three attempts in a few seconds, while a gateway that
  takes a minute to answer 504 is abandoned after one try rather than three.

## Install

```bash
pip install tasmac-mcp
```

That gives you two commands: `tasmac` (the CLI) and `tasmac-mcp` (the server).

You can also run it without installing anything, which is the neatest way to
wire up the MCP server:

```bash
uvx tasmac-mcp
```

Or clone the repo and run `python3 tasmac_core.py` directly. The core is
standard library only, so a checkout needs nothing installed at all.

## Finding a shop

TASMAC has no pincode field, so an area or pincode is geocoded through
OpenStreetMap Nominatim first, then handed to the nearby-shops endpoint.
Distances are straight line from that point, not driving distance. Nominatim
asks for one request per second and a real User-Agent, both of which the core
respects. If it is unreachable and the place happens to be a taluka name, the
finder falls back to listing that taluka.

```bash
python3 tasmac_core.py --find 600119            # pincode, nearest first
python3 tasmac_core.py --find "Thiruvanmiyur"   # area or landmark
python3 tasmac_core.py --district Chennai       # whole district by taluka
python3 tasmac_core.py --near 12.9010,80.2279   # raw coordinates
```

## Finding a bottle

The other direction: which shop near me has this.

```bash
python3 tasmac_core.py --product "Sula Brut" --find 600119
python3 tasmac_core.py --product "Old Monk" --find "Velachery" --limit 8
python3 tasmac_core.py --product "jacobs creek chardonnay" --near 12.90,80.22
```

Names are matched on words in any order, ignoring punctuation and spacing,
because the catalogue spells the same brand both `JACOB S CREEK` and
`Jacobs Creek`. So `jacobs creek chardonnay` and `Jacob's Creek Chardonnay`
both land.

A query like `Old Monk` matches several distinct products in three pack sizes
each. Locating one variant costs one API call, so the search goes round robin
across distinct products first, up to `MAX_PRODUCT_QUERIES` (6), and lists
what it did not search. Searching the first six matches in catalogue order
would spend the whole budget on one product's pack sizes and wrongly report
the rest as unavailable.

The catalogue is statewide, so a product existing in it says nothing about it
being on a shelf near you. Only the location call answers that.

## CLI

```bash
python3 tasmac_core.py 4107                          # everything in stock
python3 tasmac_core.py 4107 --category wine          # just wine, cheapest first
python3 tasmac_core.py 4107 -c wine --max-price 2500
python3 tasmac_core.py 4107 -q "sula" --all          # name search, incl. sold out
python3 tasmac_core.py 4107 --sort stock --limit 20
python3 tasmac_core.py 4107 --json                   # raw, for piping

python3 tasmac_core.py 4107 --changes                # diff vs previous snapshot
python3 tasmac_core.py 4107 --changes -c wine --since 2026-08-01
python3 tasmac_core.py 4107 --history "vina sol"     # price and stock over time
```

## MCP

```bash
claude mcp add tasmac -- uvx tasmac-mcp
```

Or in a client config (Claude Desktop, or anything else that speaks MCP):

```json
{ "mcpServers": { "tasmac": { "command": "uvx", "args": ["tasmac-mcp"] } } }
```

From a checkout instead, point at the shim: `python3 /path/to/mcp_server.py`.

**Requires mcp 1.x.** Version 2.0 of the SDK removed `mcp.server.fastmcp` in
favour of `MCPServer`, so the dependency is pinned to `<2` until the server is
ported.

Tools: `tasmac_find_shop`, `tasmac_find_product`, `tasmac_stock`,
`tasmac_changes`, `tasmac_history`, `tasmac_snapshots`.

The server advertises an icon (`icon.svg`, a bottle neck over a map pin: what
it is, and what it tells you) along with its repo URL and real version. The
icon ships as a data URI so it resolves over stdio, where there is no host to
fetch an image from. Edit `icon.svg` and regenerate the constant in
`tasmac_mcp/server.py`; a test fails if the two drift apart.

Every tool takes `format`: `text` (default) for a compact table, or `json` for
the full structured result. The table truncates long product names and
addresses and omits fields such as `product_id`, `pack_size`, `supplier` and
coordinates, so use `json` when an agent needs to compute over the rows rather
than report them. Errors come back as `{"error": "..."}` under `json`, so a
caller parsing the output never hits a bare string. The CLI equivalent is
`--json`, which works in every mode.

If you would rather have a slash command than an MCP server, copy
`claude-code/tasmac.md` into `~/.claude/commands/` and set the path inside it.

## History

Every lookup writes a dated snapshot to `history.db` (SQLite). Installed, that
lives in `~/.local/share/tasmac-mcp/`; from a checkout that already has one at
its root, that file keeps being used. Override either with `TASMAC_DB`. Re-running on the same day overwrites that day. So
the archive builds itself simply by using the tool, and `--changes` starts
working from the second day onward.

`--changes` reports four things between two snapshots: new on shelf, sold out,
price changed, and stock moved.

Set `TASMAC_NO_HISTORY=1` to disable the write and keep lookups read-only.

Tables: `snapshots(shop, taken_on, product_id, name, category, origin, unit,
mrp, stock)` and `runs(shop, taken_on, fetched_at, skus, in_stock)`.

## Tests

```bash
python3 -m unittest discover -s tests -v   # offline, no network, ~2ms
python3 tests/live_check.py                # live canary against the real API
```

The offline suite proves the code is right. Every test in it exists because
something actually broke: duplicated rows on two endpoints, a brand the
catalogue spells two ways, a search that reported a bottle unavailable while it
sat a kilometre away, shops filed in the wrong district.

The canary proves the *API* still behaves the way the code assumes, which is
the failure mode that matters here. It separates two things: a broken contract
(FAIL, someone must act) from upstream simply being down or having changed in a
way the code already absorbs (NOTE). Transient 5xx are retried before anything
is called a failure. It runs daily in GitHub Actions and opens an issue when
the contract genuinely breaks, closing it again when it recovers.

## Hosting it for other people

Local installs are the default and the better experience: each person's
requests come from their own machine and their snapshot history is genuinely
theirs. But an MCP client that only accepts a URL (claude.ai's custom
connectors, for one) needs a hosted instance.

```bash
docker build -t tasmac-mcp .
docker run -p 8080:8080 tasmac-mcp
# then point a client at http://localhost:8080/mcp
```

The image runs `tasmac-mcp-http`, which serves streamable HTTP on `PORT`
(default 8080) at `MCP_PATH` (default `/mcp`), so it drops onto Cloud Run, Fly,
Render or Railway unchanged.

Hosted mode deliberately differs from local in two ways:

- **Snapshot history is off.** A shared archive is nobody's history. Rather
  than answer misleadingly, `tasmac_changes` and `tasmac_history` explain that
  they need a local install.
- **Upstream responses are cached** for `TASMAC_CACHE_TTL` seconds (default
  900). This is not an optimisation, it is the thing that makes hosting
  defensible: one server answering many people would otherwise concentrate
  every request onto a single IP, against endpoints that already return 504
  under their own load. TASMAC refreshes stock roughly daily, so fifteen
  minutes of staleness costs nothing real.

If you do host a public instance, watch what it does to TASMAC before you
advertise it widely.

## Caching

The product catalogue is cached in `history.db` for 7 days
(`PRODUCT_CACHE_DAYS`), since it is a 360KB call that rarely changes. District,
taluk and per-shop address lookups are cached for the life of the process.
Upstream HTTP responses are cached in memory for `TASMAC_CACHE_TTL` seconds
(default 900, set 0 to disable), bounded to `CACHE_MAX` entries because a full
shop payload is around 550KB.

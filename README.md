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
- **The shop directory and the stock table disagree.** Shops 4511 and 971 both
  carry addresses and coordinates in the directory and return nothing at all
  from stock. There is no referential integrity between the two. A lookup for
  one of these says so rather than claiming the shop does not exist.
- **There is no TEQUILA category.** 41 tequilas sit in LIQUOR, WHISKY and WINE,
  and all 13 sojus are filed as WINE, so filtering `category=WINE` returns
  tequila while tequila itself is unfindable. This tool re-files both into
  derived `TEQUILA` and `SOJU` categories. Those rules are ours, not TASMAC's,
  and `raw_category` always keeps what they actually said.
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

Not on PyPI yet, so install straight from GitHub:

```bash
pip install git+https://github.com/notprashanth/tasmac-mcp
```

That gives you two commands: `tasmac` (the CLI) and `tasmac-mcp` (the server).

You can also run it without installing anything, which is the neatest way to
wire up the MCP server:

```bash
uvx --from git+https://github.com/notprashanth/tasmac-mcp tasmac-mcp
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
claude mcp add tasmac -- uvx --from git+https://github.com/notprashanth/tasmac-mcp tasmac-mcp
```

Or in a client config (Claude Desktop, or anything else that speaks MCP):

```json
{
  "mcpServers": {
    "tasmac": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/notprashanth/tasmac-mcp",
        "tasmac-mcp"
      ]
    }
  }
}
```

From a checkout instead, point at the shim: `python3 /path/to/mcp_server.py`.

### Hosted instance

There is a public one, so a client that only accepts a URL needs nothing
installed:

```
https://tasmac-mcp-165413301348.asia-south1.run.app/mcp
```

It runs the same code with snapshot history off and upstream responses cached,
for the reasons under [Hosting it for other people](#hosting-it-for-other-people).
A local install is still the better experience: the history tools only mean
something when the archive is yours.

The repo also ships `plugin.json` and `mcp.json` for
[agent-plugins.org](https://agent-plugins.org), which declare that hosted
endpoint. They package it for discovery; they do not change how it works.

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

`tasmac_stock`, `tasmac_find_shop` and `tasmac_find_product` take
`count_only`, which answers "how many" without paying for the rows. Counting
the elite shops in Chennai used to mean pulling all 375 records to read one
boolean per row.

JSON output drops nulls, internal ids (`taluka_id`, `district_id`) and
`misfiled` when false, so the payload carries only what a caller can act on.

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

## Shop tiers

TASMAC tags some shops `elite`. That is a licence class, not a description of
what is on the shelf. Surveyed statewide, **63 of 157 elite shops stock nothing
at all above Rs 3,000**, so sending someone to the nearest elite shop often
sends them to two fortified wines.

So the tier is computed from inventory instead, and shipped in
`tasmac_mcp/data/tiers.json`:

| Tier | Meaning |
|---|---|
| `flagship` | 120+ lines over Rs 3,000, and 8+ over Rs 10,000 |
| `premium` | 40+ lines over Rs 3,000 |
| `standard` | 5+ |
| `basic` | fewer than 5 |
| `no_stock` | in the directory, returns no inventory |

```bash
python3 tasmac_core.py --find 600119 --tier premium
```

Statewide there are **36 premium-or-better shops**, concentrated in seven
districts: Chennai 20, Coimbatore 7, Chengalpattu 3, Madurai 2, Salem 2,
Nilgiris 1, Tiruppur 1. The other 31 districts have none.

**A shop with no tier was never surveyed, which is not the same as basic.**
Coverage is every shop in Chennai plus every elite-tagged shop statewide. The
shortcut rests on one finding: across all 375 Chennai shops, no shop with 40+
premium lines lacked the elite tag. That is one district's evidence, so treat
it as an assumption rather than a fact.

`scripts/survey.py` recomputes the file:

```bash
python3 scripts/survey.py --scope nightly   # ~79 known premium/standard shops, ~45s
python3 scripts/survey.py --scope elite     # ~200 elite shops statewide, ~2 min
```

**It has to run from India.** A GitHub Actions run managed zero of 79 shops in
nineteen minutes before being cancelled, against 45 seconds from Chennai:
TASMAC is unusable at any practical speed from US egress. The workflow exists
but its schedule is switched off and it is dispatch-only. Scheduling this
properly means a Cloud Run job in `asia-south1`, alongside the hosted server.

There is deliberately no full 4,852-shop pass on any schedule.

## Rarity

`tasmac_find_product` reports how widely a bottle is carried, because "something
I can't get elsewhere" is unanswerable from the API alone: it would take one
call per shop. The survey already opens every elite shop in the state, so the
count comes free.

```
Bruichladdich The Classic Laddie 700ml: carried by 4 of 157 surveyed shops (uncommon)
```

Bands are `rare` (3 shops or fewer), `uncommon` (up to 15%), `common`, and
`everywhere`. **Counts are over surveyed shops only.** A bottle absent from
`data/rarity.json` was not seen in any surveyed shop, which is not the same as
being unavailable in Tamil Nadu.

## Is the price fair

MRP alone ranks a Rs 19,120 Yamazaki above a Rs 10,120 Bowmore, while being
both the worse whisky and the worse buy. So premium bottles carry a comparison
against Indian duty free, per 750ml:

```
MRP   PRODUCT                          SIZE   STOCK  ORIGIN  VS DF
5340  CHIVAS REGAL AGED 12 YO          750ml  25     IFL     1.5x
```

Duty free rather than US retail because 447 of TASMAC's 451 lines above
Rs 3,000 are imported, it is quoted in rupees so no exchange rate drifts under
the ratio, it is roughly the product without excise, and it is the buyer's
actual alternative: the next flight.

Across matched bottles the median is **1.4x** with a range of 0.7x to 2.4x.

**Coverage is deliberately thin: 40 bottles of 451.** Matching is the hard part,
not fetching. Early attempts produced Louis XIII at 99.6x (matched to a cheaper
Rémy), Lagavulin 16 at 0.1x (matched to a 75ml miniature scaled up), and three
Jack Daniel's flavours all priced off a twin pack. A match now requires
identical brand tokens in both directions, an exact age statement, an exact
cognac grade, a bottle between 500ml and 1500ml, and no bundle or multipack
wording. Everything else is left blank, because a confident wrong number is
worse than a gap. `scripts/reference_prices.py` rebuilds the table.

## A stock number is not a rate

One reading is restock size minus sales since restock, and those two cannot be
separated from a single observation. So 44 bottles is not evidence of brisk
turnover and 51 is not evidence of a dud, and "18 in stock" is not a promise
that 18 are there when you arrive. The tool says so rather than letting the
number be read as velocity, which is a mistake that is easy to make in both
directions in the same breath.

Separating those two terms needs snapshots over time, which is what the history
tools would give a local install.

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

Hosted mode deliberately differs from local in three ways:

- **Snapshot history is off**, and the reason is persistence rather than
  remoteness. A container with an ephemeral disk loses the archive on every
  restart, which is equally true on a laptop with an unwritable path. All
  three history tools run one shared capability check and say so; point
  `TASMAC_DB` at durable storage and they work remotely too.
- **Upstream responses are cached** for `TASMAC_CACHE_TTL` seconds (default
  900). This is not an optimisation, it is the thing that makes hosting
  defensible: one server answering many people would otherwise concentrate
  every request onto a single IP, against endpoints that already return 504
  under their own load. TASMAC refreshes stock roughly daily, so fifteen
  minutes of staleness costs nothing real.
- **DNS-rebinding protection is off**, because it protects nothing here. See
  below, because getting this wrong is silent.

If you do host a public instance, watch what it does to TASMAC before you
advertise it widely.

### The two headers that break a hosted MCP server

The SDK enables DNS-rebinding protection whenever the server looks like it is
on localhost, and it checks two headers. Both fail in a way that is easy to
miss:

- **`Host`** is matched against the allow-list, so a deployed instance answers
  **HTTP 421 Invalid Host header** to its own hostname.
- **`Origin`** is matched too, so a browser-based client such as claude.ai's
  connector, which sends `Origin: https://claude.ai`, gets **HTTP 403 Invalid
  Origin header** and simply spins.

A request with no `Origin` header at all passes both checks. curl sends none,
so **curl will report a perfectly healthy server that no real client can
use.** Test with the header, or you are testing nothing:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Origin: https://claude.ai' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"c","version":"0"}}}'
```

`tasmac-mcp-http` therefore turns the protection off by default. That is the
honest setting for this server: the protection exists to stop a web page
reaching something bound to your own loopback, and a public URL serving
read-only lookups over public data is reachable by anyone already. Nothing that
writes is registered in hosted mode.

To narrow it anyway, set both allow-lists. `TASMAC_ALLOWED_HOSTS` and
`TASMAC_ALLOWED_ORIGINS` take comma-separated values, and `*` on either means
off. There is no wildcard entry: the SDK matches exactly, plus a `host:*` port
pattern.

```bash
docker run -p 8080:8080 \
  -e TASMAC_ALLOWED_HOSTS=tasmac-mcp-xxxx.asia-south1.run.app \
  -e TASMAC_ALLOWED_ORIGINS=https://claude.ai \
  tasmac-mcp
```

### Cloud Run

```bash
gcloud run deploy tasmac-mcp --source . --region=asia-south1 \
  --allow-unauthenticated --memory=512Mi
```

`asia-south1` because the upstream API is in India. Scale-to-zero, so an idle
instance costs nothing. `--max-instances=1 --session-affinity` because the
server keeps MCP sessions, and a session lives on the instance that issued it.

**Do not run this stateless.** It is the obvious setting for a public endpoint
and it breaks claude.ai's connector: a stateless server issues no
`Mcp-Session-Id`, and the connector needs that header to make any request after
`initialize`. What you see is `POST /mcp` answering **200** in the logs, three
retries, and a client-side "couldn't reach the server" that points nowhere. A
stateless `GET` on the endpoint hangs open rather than answering, which is the
same problem seen from the other side. The Python SDK client tolerates both, so
it will tell you everything is fine. Check the header instead:

```bash
curl -sD - -o /dev/null -X POST <url>/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"c","version":"0"}}}' \
  | grep -i mcp-session-id
``` The container already sets `PORT`, `HOST`, `MCP_PATH`,
`TASMAC_CACHE_TTL` and `TASMAC_NO_HISTORY`, and Cloud Run overrides `PORT`
itself, so no `--set-env-vars` is needed.

In a fresh project the default compute service account has no build
permissions, and a `--source` deploy fails at source upload rather than at
build, which reads like a bug in your Dockerfile. Grant these first:

```bash
PROJECT=$(gcloud config get-value project)
NUM=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
for role in cloudbuild.builds.builder storage.objectViewer \
            artifactregistry.writer logging.logWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${NUM}-compute@developer.gserviceaccount.com" \
    --role="roles/$role" --condition=None >/dev/null
done
```

## Caching

The product catalogue is cached in `history.db` for 7 days
(`PRODUCT_CACHE_DAYS`), since it is a 360KB call that rarely changes. District,
taluk and per-shop address lookups are cached for the life of the process.
Upstream HTTP responses are cached in memory for `TASMAC_CACHE_TTL` seconds
(default 900, set 0 to disable), bounded to `CACHE_MAX` entries because a full
shop payload is around 550KB.

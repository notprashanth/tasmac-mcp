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

The parameter names are the only hard part, and they are inconsistent across
the very same API. Stock wants `i_ShopNumber` and 409s on `shopNumber`,
`p_shopNumber` and `p_shop_number`. District stock wants `p_districtId`, not
`districtId`. Taluka wants `i_TalukaId`, not `i_TalukId` (which is the spelling
in the site's own service method name). Nearby wants `p_latitude` and
`p_longitude` **as strings**, and 409s on numbers. The 409 body helpfully lists
the required names, so probing is quick.

Other endpoints seen in the bundle but not used here: `liquor/get-brandList`,
`liquor/get-productList`, `liquor/get-productListByBrand`, `liquor/get-stockDetails`,
`liquor/get-stockDetailsBy-DistrictId-talukId`,
`liquor/get-stockDetailsBy-ProductId/lat-long` (which shop near me has this
product), and an `fl11` variant of the same for bars.

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

## Install

Nothing to install for the CLI. Python 3.9+, stdlib only.

The MCP server needs the official SDK:

```bash
pip install mcp
```

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
claude mcp add tasmac -- python3 /absolute/path/to/tasmac-mcp/mcp_server.py
```

Or in a client config (Claude Desktop, or anything else that speaks MCP):

```json
{ "mcpServers": { "tasmac": { "command": "python3",
  "args": ["/absolute/path/to/tasmac-mcp/mcp_server.py"] } } }
```

Tools: `tasmac_find_shop`, `tasmac_stock`, `tasmac_changes`, `tasmac_history`,
`tasmac_snapshots`.

If you would rather have a slash command than an MCP server, copy
`claude-code/tasmac.md` into `~/.claude/commands/` and set the path inside it.

## History

Every lookup writes a dated snapshot to `history.db` (SQLite, same folder,
override with `TASMAC_DB`). Re-running on the same day overwrites that day. So
the archive builds itself simply by using the tool, and `--changes` starts
working from the second day onward.

`--changes` reports four things between two snapshots: new on shelf, sold out,
price changed, and stock moved.

Set `TASMAC_NO_HISTORY=1` to disable the write and keep lookups read-only.

Tables: `snapshots(shop, taken_on, product_id, name, category, origin, unit,
mrp, stock)` and `runs(shop, taken_on, fetched_at, skus, in_stock)`.

## Not built

Product-first search: "which shop near me has Sula Sauvignon Blanc". The
`liquor/get-stockDetailsBy-ProductId/lat-long` endpoint is built for exactly
this and pairs with `get-productList`, so it is a small addition whenever it
is wanted.

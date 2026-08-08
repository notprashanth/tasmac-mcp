Check live stock and prices at a TASMAC shop. Usage: `/tasmac <shop-number|area|pincode> [category or question]`

---

Drop this file in `~/.claude/commands/tasmac.md` to get a `/tasmac` slash
command in Claude Code. Set `TASMAC` below to wherever you cloned the repo.
This is an alternative to the MCP server, not a companion to it: use whichever
suits you.

```
TASMAC=/absolute/path/to/tasmac-mcp/tasmac_core.py
```

## Step 0 — Resolve the shop

If `$ARGUMENTS` names a place rather than a shop number (an area, a district or
a six digit pincode), find the shop first:

```bash
python3 $TASMAC --find "<area or pincode>"
python3 $TASMAC --district "<name>"
```

Take the nearest shop and say which one you picked. Shops marked `(?)` are
misfiled by TASMAC, so skip them.

## Step 1 — Fetch

```bash
python3 $TASMAC <shop> [-c <category>] [--max-price N] [-q <search>]
```

Categories: `wine`, `whisky`, `brandy`, `rum`, `gin`, `vodka`, `beer`, `liquor`.
Omit `-c` if the ask is broad. Use `-q` for a specific bottle. For what changed,
use `--changes`. For one product over time, use `--history "<name>"`.

## Step 2 — Answer

Lead with a recommendation, not the raw table. Rank on quality per rupee, not
price alone, and say why in one line each. Show at most 10 rows, always with
MRP and stock count.

Flag these when relevant:
- Items TASMAC has miscategorised. Soju, tequila and flavoured drinks sit under
  WINE. Do not pass them off as wine.
- Low stock (under 5) if it is a top pick.
- Warm storage risk for white, sparkling and delicate wine. TASMAC shops are
  not temperature controlled.

## Step 3 — Close

One line: the pick, the price, and the shop.

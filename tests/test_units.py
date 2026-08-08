#!/usr/bin/env python3
"""Offline invariant tests. Standard library only, no network.

Run:  python3 -m unittest discover -s tests -v

Every test here exists because something actually broke. The two dedupe tests
are regressions from 2026-08-08, when the upstream API started returning each
in-stock row twice on one endpoint and three times on another. The output
stayed plausible, just doubled and tripled, which is exactly the kind of bug a
person does not catch by reading a table.
"""
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tasmac_mcp import core


def shop_payload(rows):
    return {"data": [{"shopNumber": "4107", "districtId": 3, "districtName": "Chennai",
                      "last_updated_time": None, "Stock_details": rows}]}


def sku(pid, name="TEST WINE", brand="OTHER-WINE", stock=10, mrp=1000, unit="750ml"):
    return {"productId": pid, "productName": name, "brandName": brand, "unitName": unit,
            "packSize": 12, "currentStock": stock, "mrpPerBottle": mrp,
            "supplierId": 1, "supplierName": "TEST SUPPLIER"}


class DedupeShopEndpoint(unittest.TestCase):
    """Regression: the shop endpoint returned every in-stock row twice."""

    def setUp(self):
        core._cache.clear()

    def test_duplicate_rows_collapse(self):
        rows = [sku(1), sku(1), sku(2), sku(2), sku(3)]          # 3 distinct, 5 rows
        with patch.object(core, "_post", return_value=shop_payload(rows)), \
             patch.object(core, "shop_info", return_value=None):
            data = core.fetch_shop("4107", write_history=False)
        ids = [i["product_id"] for i in data["items"]]
        self.assertEqual(len(ids), 3, "duplicate products were not collapsed")
        self.assertEqual(len(ids), len(set(ids)), "product_id repeated in output")

    def test_first_copy_wins_and_values_survive(self):
        rows = [sku(1, stock=42, mrp=1340), sku(1, stock=42, mrp=1340)]
        with patch.object(core, "_post", return_value=shop_payload(rows)), \
             patch.object(core, "shop_info", return_value=None):
            items = core.fetch_shop("4107", write_history=False)["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["stock"], 42)
        self.assertEqual(items[0]["mrp"], 1340)

    def test_rows_without_a_product_id_are_kept(self):
        rows = [sku(None), sku(None), sku(7)]
        with patch.object(core, "_post", return_value=shop_payload(rows)), \
             patch.object(core, "shop_info", return_value=None):
            items = core.fetch_shop("4107", write_history=False)["items"]
        self.assertEqual(len(items), 3, "null product_id rows must not collapse into one")


class DedupeProductEndpoint(unittest.TestCase):
    """Regression: the product location endpoint tripled every (shop, product)."""

    def setUp(self):
        core._cache.clear()

    def _payload(self, triples):
        return {"data": [{"shopNumber": s, "latitude": "12.9", "longitude": "80.2",
                          "km": km, "talukaId": 27, "talukaName": "Sholinganallur",
                          "districtName": "Chennai",
                          "Stock_details": sku(pid, stock=st)} for s, pid, km, st in triples]}

    def test_repeated_shop_product_pairs_collapse(self):
        rows = [(715, 2434, 2.37, 3)] * 3 + [(4498, 2434, 2.42, 49)] * 3
        with patch.object(core, "_post", return_value=self._payload(rows)), \
             patch.object(core, "shops_in_taluk", return_value=[]):
            out = core.product_shops(2434, 12.9, 80.2, limit=10)
        self.assertEqual(len(out), 2, "tripled rows were not collapsed")
        self.assertEqual(len({(s["shop"], s["product"]) for s in out}), 2)

    def test_same_product_at_different_shops_is_not_collapsed(self):
        rows = [(715, 2434, 2.37, 3), (4498, 2434, 2.42, 49), (4092, 2434, 2.62, 56)]
        with patch.object(core, "_post", return_value=self._payload(rows)), \
             patch.object(core, "shops_in_taluk", return_value=[]):
            out = core.product_shops(2434, 12.9, 80.2, limit=10)
        self.assertEqual(len(out), 3, "distinct shops must survive dedupe")

    def test_out_of_stock_is_dropped_and_order_is_by_distance(self):
        rows = [(4092, 2434, 6.9, 56), (715, 2434, 2.3, 0), (4498, 2434, 2.4, 49)]
        with patch.object(core, "_post", return_value=self._payload(rows)), \
             patch.object(core, "shops_in_taluk", return_value=[]):
            out = core.product_shops(2434, 12.9, 80.2, limit=10)
        self.assertEqual([s["shop"] for s in out], ["4498", "4092"])


class ProductNameMatching(unittest.TestCase):
    """Regression: the catalogue spells one brand two ways and drops apostrophes."""

    CATALOGUE = [
        {"product_id": 1, "name": "JACOB S CREEK CHARDONNAY", "category": "WINE",
         "origin": "IFL", "unit": "750ml", "mrp": 2180, "pack_size": 12,
         "supplier": "x", "supplier_type": "IFL"},
        {"product_id": 2, "name": "Jacobs Creek Merlot", "category": "WINE",
         "origin": "IFL", "unit": "750ml", "mrp": 2200, "pack_size": 12,
         "supplier": "x", "supplier_type": "IFL"},
        {"product_id": 3, "name": "Sula Vineyards Sauvignon Blanc", "category": "WINE",
         "origin": "OTHER", "unit": "750ml", "mrp": 1340, "pack_size": 12,
         "supplier": "x", "supplier_type": "OTHER"},
        {"product_id": 4, "name": "OLD MONK DELUX RUM", "category": "RUM",
         "origin": "", "unit": "750ml", "mrp": 800, "pack_size": 12,
         "supplier": "x", "supplier_type": ""},
    ]

    def setUp(self):
        core._cache.clear()

    def search(self, q, **kw):
        with patch.object(core, "products", return_value=self.CATALOGUE):
            return core.search_products(q, **kw)

    def test_apostrophe_is_ignored(self):
        self.assertTrue(any("JACOB S CREEK" in p["name"] for p in self.search("Jacob's Creek")))

    def test_missing_space_still_matches(self):
        names = [p["name"] for p in self.search("jacobs creek chardonnay")]
        self.assertIn("JACOB S CREEK CHARDONNAY", names)

    def test_words_match_in_any_order(self):
        names = [p["name"] for p in self.search("sauvignon sula")]
        self.assertIn("Sula Vineyards Sauvignon Blanc", names)

    def test_category_filter_applies(self):
        self.assertEqual(self.search("old monk", category="WINE"), [])
        self.assertEqual(len(self.search("old monk", category="RUM")), 1)

    def test_nonsense_matches_nothing(self):
        self.assertEqual(self.search("chateau lafite rothschild"), [])


class ProductSearchFanOut(unittest.TestCase):
    """Regression: 'Old Monk' reported unavailable while sitting 1km away.

    Six matches across two products in three sizes each. Searching in catalogue
    order spends the whole budget on one product's pack sizes and wrongly calls
    the other unavailable, so the fan-out must spread across distinct names
    before it goes wider on sizes.
    """

    def _catalogue(self):
        out = []
        pid = 1
        for name in ("OLD MONK DELUX RUM", "OLD MONK GOLD RESERVE RUM"):
            for unit in ("180ml", "375ml", "750ml"):
                out.append({"product_id": pid, "name": name, "category": "RUM", "origin": "",
                            "unit": unit, "mrp": 500, "pack_size": 12,
                            "supplier": "x", "supplier_type": ""})
                pid += 1
        return out

    def setUp(self):
        core._cache.clear()

    def test_first_lookups_span_both_products(self):
        queried = []

        def fake_shops(pid, lat, lon, limit=10):
            queried.append(pid)
            return []

        with patch.object(core, "products", return_value=self._catalogue()), \
             patch.object(core, "geocode", return_value=(12.9, 80.2, "somewhere")), \
             patch.object(core, "product_shops", side_effect=fake_shops):
            core.find_product("old monk", area="Velachery")

        names = {p["name"] for p in self._catalogue() if p["product_id"] in queried[:3]}
        self.assertEqual(len(names), 2, f"first three lookups hit only {names}")
        self.assertLessEqual(len(queried), core.MAX_PRODUCT_QUERIES)


class MisfiledShops(unittest.TestCase):
    """Government district tags are wrong for a slice of shops; flag the outliers."""

    def shop(self, num, lat, lon):
        return {"shop": str(num), "address": "", "taluka": "T", "taluka_id": 1,
                "district": "D", "district_id": 1, "lat": str(lat), "lon": str(lon),
                "elite": False, "km": None}

    def test_far_away_shop_is_flagged(self):
        shops = [self.shop(1, 11.10, 79.10), self.shop(2, 11.11, 79.11),
                 self.shop(3, 11.12, 79.12), self.shop(57, 13.117, 80.284)]
        flagged = [s["shop"] for s in core._flag_misfiled(shops) if s.get("misfiled")]
        self.assertEqual(flagged, ["57"])

    def test_tight_cluster_flags_nothing(self):
        shops = [self.shop(i, 13.0 + i / 100, 80.2) for i in range(5)]
        self.assertFalse(any(s.get("misfiled") for s in core._flag_misfiled(shops)))


class Filters(unittest.TestCase):
    ITEMS = [
        {"product_id": 1, "name": "Cheap Wine", "category": "WINE", "origin": "",
         "unit": "750ml", "mrp": 500, "stock": 5, "pack_size": 12, "supplier": ""},
        {"product_id": 2, "name": "Posh Wine", "category": "WINE", "origin": "",
         "unit": "750ml", "mrp": 5000, "stock": 2, "pack_size": 12, "supplier": ""},
        {"product_id": 3, "name": "Sold Out Wine", "category": "WINE", "origin": "",
         "unit": "750ml", "mrp": 900, "stock": 0, "pack_size": 12, "supplier": ""},
        {"product_id": 4, "name": "Some Whisky", "category": "WHISKY", "origin": "",
         "unit": "750ml", "mrp": 1500, "stock": 9, "pack_size": 12, "supplier": ""},
    ]

    def test_in_stock_only_by_default(self):
        got = core.filter_items(self.ITEMS)
        self.assertNotIn(3, [i["product_id"] for i in got])

    def test_category_and_price_window(self):
        got = core.filter_items(self.ITEMS, category="WINE", max_price=1000)
        self.assertEqual([i["product_id"] for i in got], [1])

    def test_sorted_cheapest_first(self):
        got = core.filter_items(self.ITEMS, category="WINE")
        self.assertEqual([i["mrp"] for i in got], sorted(i["mrp"] for i in got))


class Retries(unittest.TestCase):
    """The heavy endpoints 504 under load, and a single 504 is usually gone
    seconds later. But a 409 means the parameter contract changed, and no
    amount of waiting fixes that."""

    def setUp(self):
        core._cache.clear()
        self.slept = []

    def _urlopen_raising(self, *errors):
        """Yield each error in turn, then a good response."""
        seq = list(errors)

        class Resp:
            def read(self_inner):
                return b'{"status": true, "data": [1]}'

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        def fake(req, timeout=None):
            if seq:
                raise seq.pop(0)
            return Resp()

        return fake

    def http_error(self, code):
        import io
        return urllib.error.HTTPError("u", code, "boom", {}, io.BytesIO(b"gateway"))

    def test_retries_a_504_then_succeeds(self):
        fake = self._urlopen_raising(self.http_error(504), self.http_error(504))
        with patch.object(core.urllib.request, "urlopen", fake), \
             patch.object(core.time, "sleep", self.slept.append):
            body = core._post("liquor/x", {})
        self.assertEqual(body["data"], [1])
        self.assertEqual(self.slept, [2, 5], "should back off between attempts")

    def test_gives_up_after_the_last_attempt(self):
        fake = self._urlopen_raising(*(self.http_error(503) for _ in range(5)))
        with patch.object(core.urllib.request, "urlopen", fake), \
             patch.object(core.time, "sleep", self.slept.append):
            with self.assertRaises(RuntimeError) as cm:
                core._post("liquor/x", {})
        self.assertIn("after 3 attempt", str(cm.exception))
        self.assertEqual(len(self.slept), 2, "three attempts means two waits")

    def test_a_409_is_never_retried(self):
        fake = self._urlopen_raising(*(self.http_error(409) for _ in range(5)))
        with patch.object(core.urllib.request, "urlopen", fake), \
             patch.object(core.time, "sleep", self.slept.append):
            with self.assertRaises(RuntimeError) as cm:
                core._post("liquor/x", {})
        self.assertIn("409", str(cm.exception))
        self.assertEqual(self.slept, [], "a contract error must fail immediately")

    def test_network_errors_are_retried(self):
        fake = self._urlopen_raising(urllib.error.URLError("no route"))
        with patch.object(core.urllib.request, "urlopen", fake), \
             patch.object(core.time, "sleep", self.slept.append):
            body = core._post("liquor/x", {})
        self.assertEqual(body["data"], [1])
        self.assertEqual(self.slept, [2])

    def test_deadline_stops_further_retries(self):
        fake = self._urlopen_raising(*(self.http_error(504) for _ in range(5)))
        with patch.object(core, "DEADLINE", 0), \
             patch.object(core.urllib.request, "urlopen", fake), \
             patch.object(core.time, "sleep", self.slept.append):
            with self.assertRaises(RuntimeError):
                core._post("liquor/x", {})
        self.assertEqual(self.slept, [], "past the deadline it must not wait at all")

    def test_a_slow_attempt_prevents_the_next_one(self):
        """The deadline must bound the whole call, not just the waits.

        Regression: with the check only guarding the sleep, three attempts that
        each took a minute ran for 188s under a 150s deadline.
        """
        clock = {"t": 0.0}
        errors = [self.http_error(504) for _ in range(5)]

        def slow_urlopen(req, timeout=None):
            clock["t"] += 100.0                      # each attempt burns 100s
            raise errors.pop(0)

        with patch.object(core, "DEADLINE", 150), \
             patch.object(core.time, "monotonic", lambda: clock["t"]), \
             patch.object(core.urllib.request, "urlopen", slow_urlopen), \
             patch.object(core.time, "sleep", self.slept.append):
            with self.assertRaises(RuntimeError) as cm:
                core._post("liquor/x", {})
        self.assertEqual(len(errors), 3, "should have stopped after two attempts")
        self.assertIn("deadline", str(cm.exception))


class CategoryParsing(unittest.TestCase):
    def test_prefixes_split_into_category_and_origin(self):
        self.assertEqual(core._split_category("OTHER-WINE"), ("WINE", "OTHER"))
        self.assertEqual(core._split_category("IFL-BEER"), ("BEER", "IFL"))
        self.assertEqual(core._split_category("WINE"), ("WINE", ""))

    def test_all_wine_variants_share_one_category(self):
        cats = {core._split_category(b)[0] for b in ("WINE", "IFL-WINE", "OTHER-WINE")}
        self.assertEqual(cats, {"WINE"})


if __name__ == "__main__":
    unittest.main(verbosity=2)

import csv
import tempfile
import unittest
from pathlib import Path

from schema_compass.catalog import Catalog
from schema_compass.engine import MatchEngine
from schema_compass.evaluate import evaluate
from schema_compass.feedback import FeedbackStore
from schema_compass.knowledge import KnowledgeBase
from schema_compass.text import normalize, role, type_compatibility

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"


def engine(knowledge=True, feedback=None):
    kb = KnowledgeBase()
    if knowledge:
        kb.add_path(DEMO / "knowledge")
    return MatchEngine(Catalog.from_csv(DEMO / "source_metadata.csv", "source"),
                       Catalog.from_csv(DEMO / "target_metadata.csv", "target"),
                       knowledge=kb, feedback=feedback)


class TextTests(unittest.TestCase):
    def test_abbreviations_and_plurals(self):
        self.assertIn("customer", normalize("CUST_ID"))
        self.assertIn("order", normalize("orders"))
        self.assertIn("address", normalize("addresses"))
        self.assertEqual(normalize("ORD_NBR", synonyms=False), "order number")

    def test_roles(self):
        self.assertEqual(role("product_name"), "name")
        self.assertEqual(role("SKU_ID"), "identifier")
        self.assertEqual(role("is_active"), "flag")
        self.assertEqual(role("TOT_AMT"), "amount")

    def test_type_compatibility(self):
        self.assertEqual(type_compatibility("NUMBER(12,2)", "DECIMAL(12,2)")[0], 1.0)
        self.assertEqual(type_compatibility("NUMBER(18,0)", "BIGINT")[0], 1.0)
        score, note = type_compatibility("VARCHAR(1)", "BOOLEAN")
        self.assertEqual(score, 0.7)
        self.assertIn("mapping", note)
        self.assertEqual(type_compatibility("DATE", "DECIMAL(10,2)")[0], 0.0)


class CatalogTests(unittest.TestCase):
    def test_lookup_by_short_and_logical_name(self):
        target = Catalog.from_csv(DEMO / "target_metadata.csv", "target")
        self.assertEqual(target.find_table("orders").name, "lumen.sales.orders")
        self.assertEqual(target.find_table("Customer Profile").platform, "SNOWFLAKE")
        with self.assertRaises(ValueError):
            target.find_table("does_not_exist")

    def test_duplicate_columns_rejected(self):
        rows = [dict(TABLE_PHYSICAL_NAME="a.b.c", COLUMN_PHYSICAL_NAME="x"),
                dict(TABLE_PHYSICAL_NAME="a.b.c", COLUMN_PHYSICAL_NAME="X")]
        with self.assertRaises(ValueError):
            Catalog.from_rows(rows)


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = engine()

    def match(self, table, column=None):
        t = self.engine.target.find_table(table)
        return self.engine.match_column(t, t.column(column)) if column else self.engine.match_table(t)

    def test_table_match_prefers_current_over_deprecated(self):
        result = self.match("lumen.sales.orders")
        self.assertEqual(result["candidates"][0]["source_table"]["name"], "SRC_DW.SALES.ORD_HDR")
        legacy = next(c for c in result["candidates"] if c["source_table"]["name"].endswith("ORD_HDR_LGCY"))
        self.assertTrue(any(e["strength"] < 0 for e in legacy["evidence"]))

    def test_column_uses_table_context(self):
        top = self.match("lumen.sales.orders", "customer_id")["candidates"][0]
        self.assertEqual((top["source_table"]["name"], top["source_column"]["name"]), ("SRC_DW.SALES.ORD_HDR", "CUST_ID"))

    def test_s2t_knowledge_resolves_non_obvious_name(self):
        result = self.match("CUSTOMER_PROFILE", "SIGNUP_DATE")
        self.assertEqual(result["candidates"][0]["source_column"]["name"], "CRT_DT")
        self.assertEqual(result["recommendation"]["band"], "high")

    def test_slack_steers_product_name(self):
        cands = self.match("product_catalog", "product_name")["candidates"]
        self.assertEqual(cands[0]["source_column"]["name"], "ITEM_DESC")
        long_desc = next(c for c in cands if c["source_column"]["name"] == "ITEM_LONG_DESC")
        self.assertTrue(any(e["strength"] < 0 for e in long_desc["evidence"]))

    def test_no_source_target_abstains(self):
        result = self.match("customer_reviews")
        self.assertEqual(result["recommendation"]["band"], "none")
        self.assertTrue(result["warnings"])
        self.assertEqual(result["advice"]["decision"], "no_source")

    def test_advice_is_actionable(self):
        advice = self.match("CUSTOMER_PROFILE", "MARKETING_OPT_IN")["advice"]
        self.assertTrue(advice["headline"].startswith("Use SRC_DW.CRM.CUST_MSTR.OPT_IN_FLG"))
        self.assertTrue(any("convert" in c for c in advice["checks"]))
        self.assertTrue(any("source-to-target sheet" in w for w in advice["why"]))
        steps = [s["step"] for s in self.match("orders", "order_status")["trace"]]
        self.assertEqual(steps[0], "Read the target definition")

    def test_issues_flag_width_and_type(self):
        top = self.match("CUSTOMER_PROFILE", "FIRST_NAME")["candidates"][0]
        self.assertTrue(any("width" in i for i in top["issues"]))
        opt = self.match("CUSTOMER_PROFILE", "MARKETING_OPT_IN")["candidates"][0]
        self.assertTrue(any("boolean" in i for i in opt["issues"]))

    def test_map_table_covers_every_column(self):
        t = self.engine.target.find_table("shipments")
        result = self.engine.map_table(t)
        self.assertEqual(len(result["columns"]), len(t.columns))

    def test_evaluation_quality_floor(self):
        summary = evaluate(self.engine, DEMO / "gold_mappings.csv")["summary"]
        self.assertGreaterEqual(summary["table"]["top1_accuracy"], 0.88)
        self.assertGreaterEqual(summary["column"]["top1_accuracy"], 0.9)
        self.assertGreaterEqual(summary["column"]["recall_at_k"], 0.95)


class FeedbackTests(unittest.TestCase):
    def test_rejection_and_approval_change_ranking(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FeedbackStore(Path(tmp) / "fb.db")
            e = engine(feedback=store)
            t = e.target.find_table("store_locations")
            col = t.column("REGION")
            first = e.match_column(t, col)["candidates"][0]
            store.record(target_table=t.id, target_column="REGION", source_table=first["source_table"]["id"],
                         source_column=first["source_column"]["name"], decision="rejected", reviewer="qa")
            after = e.match_column(t, col)["candidates"]
            self.assertNotEqual(after[0]["source_column"]["name"], first["source_column"]["name"])
            store.record(target_table=t.id, target_column="REGION", source_table=first["source_table"]["id"],
                         source_column=first["source_column"]["name"], decision="approved", reviewer="qa")
            again = e.match_column(t, col)["candidates"][0]
            self.assertEqual(again["source_column"]["name"], first["source_column"]["name"])
            self.assertGreaterEqual(again["score"], 97)
            out = Path(tmp) / "approved.csv"
            self.assertEqual(store.export_s2t(out), 1)
            with out.open() as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 1)


if __name__ == "__main__":
    unittest.main()

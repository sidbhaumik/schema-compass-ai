import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from schema_compass.api import create_app
from schema_compass.app import build_engine, load_settings
from schema_compass.rerank import ClaudeReranker, RerankResult, build_prompt
from schema_compass.workspace import Workspace, normalize_metadata, read_table_file

ROOT = Path(__file__).resolve().parents[1]


class FakeMessages:
    def __init__(self, result):
        self.result, self.calls = result, []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(stop_reason="end_turn", parsed_output=self.result, model=kwargs["model"],
                               usage=SimpleNamespace(input_tokens=100, output_tokens=50))


class FakeClient:
    def __init__(self, result):
        self.beta = SimpleNamespace(messages=FakeMessages(result))


def settings(tmp):
    s = load_settings(ROOT / "schema-compass.toml")
    s.feedback_db = str(Path(tmp) / "fb.db")
    return s


class RerankTests(unittest.TestCase):
    def test_llm_verdict_reorders_and_explains(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = build_engine(settings(tmp), use_llm=False)
            t = engine.target.find_table("orders")
            col = t.column("order_status")
            heuristic = engine.match_column(t, col, k=3)
            second = heuristic["candidates"][1]["source_column"]["name"]
            verdict = RerankResult(candidates=[
                dict(llm_ref="C1", verdict="not_match", confidence=10, rationale="wrong grain"),
                dict(llm_ref="C2", verdict="match", confidence=95, rationale="same lifecycle status"),
            ], no_match=False, summary="Use C2.", transformation=None)
            client = FakeClient(verdict)
            engine.reranker = ClaudeReranker(model="claude-opus-5-5", client=client)
            result = engine.match_column(t, col, k=3, rerank=True)
            self.assertTrue(result["reranked"])
            self.assertEqual(result["candidates"][0]["source_column"]["name"], second)
            self.assertEqual(result["recommendation"]["summary"], "Use C2.")
            call = client.beta.messages.calls[0]
            self.assertEqual(call["output_format"], RerankResult)
            self.assertEqual(call["fallbacks"], "default")
            self.assertIn("<candidates>", call["messages"][0]["content"])

    def test_prompt_contains_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = build_engine(settings(tmp), use_llm=False)
            t = engine.target.find_table("product_catalog")
            r = engine.match_column(t, t.column("product_name"))
            prompt = build_prompt(t, t.column("product_name"), r["candidates"], r["context"])
            self.assertIn("ITEM_LONG_DESC", prompt)
            self.assertIn("slack", prompt)


class ApiTests(unittest.TestCase):
    def test_project_match_feedback_and_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            demo = ws.seed_demo(ROOT / "demo")
            client = TestClient(create_app(ws))
            self.assertEqual(client.get("/healthz").json()["status"], "ok")
            info = client.get(f"/api/projects/{demo.id}").json()
            self.assertTrue(info["ready"])
            self.assertEqual(info["stats"]["target_tables"], 10)
            base = f"/api/projects/{demo.id}"
            r = client.post(f"{base}/match", json=dict(table="returns", column="refund_amount")).json()
            self.assertEqual(r["candidates"][0]["source_column"]["name"], "RFND_AMT")
            self.assertEqual(r["advice"]["decision"], "use")
            self.assertTrue(r["trace"])
            self.assertEqual(client.post(f"{base}/match", json=dict(table="nope")).status_code, 404)
            self.assertEqual(client.post(f"{base}/match", json=dict(table="returns", rerank=True)).status_code, 400)
            self.assertEqual(client.get("/api/projects/missing").status_code, 404)
            top = r["candidates"][0]
            ok = client.post(f"{base}/feedback", headers={"X-Forwarded-Email": "sso.user@example.com"},
                             json=dict(target_table=r["target"]["table"]["id"], target_column="refund_amount",
                                       source_table=top["source_table"]["id"], source_column="RFND_AMT",
                                       decision="approved"))
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(client.get(f"{base}/feedback").json()[0]["reviewer"], "sso.user@example.com")
            self.assertIn("RFND_AMT", client.get(f"{base}/map-table.csv", params=dict(table="returns")).text)
            self.assertIn("RFND_AMT", client.get(f"{base}/approved.csv").text)
            suggestions = client.get(f"{base}/suggest", params=dict(q="cust")).json()
            self.assertTrue(any(s["value"].endswith("CUSTOMER_PROFILE") for s in suggestions))
            self.assertEqual(client.get("/").status_code, 200)

    def test_self_service_project_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            client = TestClient(create_app(ws))
            pid = client.post("/api/projects", json=dict(name="Wave 2")).json()["id"]
            base = f"/api/projects/{pid}"
            self.assertEqual(client.post(f"{base}/match", json=dict(table="x")).status_code, 409)
            source = ("database,schema,table_name,column_name,description,data_type,length\n"
                      "SRC,CRM,CUST,CUST_NM,Customer full name,VARCHAR(80),80\n"
                      "SRC,CRM,CUST,CUST_ID,Customer key,NUMBER(18,0),\n")
            target = ("table_name,column_name,business_name,definition,datatype\n"
                      "gold.customer,customer_name,Customer Name,Full name of the customer,STRING\n"
                      "gold.customer,customer_id,Customer ID,Customer key,BIGINT\n")
            rep = client.post(f"{base}/metadata/source", files=dict(file=("src.csv", source)),
                              data=dict(platform="SNOWFLAKE")).json()
            self.assertEqual((rep["tables"], rep["columns"]), (1, 2))
            self.assertEqual(rep["mapped_headers"]["COLUMN_DESCRIPTION"], "description")
            client.post(f"{base}/metadata/target", files=dict(file=("tgt.csv", target)), data=dict(platform="DATABRICKS"))
            bad = client.post(f"{base}/metadata/target", files=dict(file=("t.csv", "foo,bar\n1,2\n")))
            self.assertEqual(bad.status_code, 400)
            self.assertIn("TABLE_PHYSICAL_NAME", bad.json()["detail"])
            s2t = "target_table,target_column,source_table,source_column\ngold.customer,customer_name,SRC.CRM.CUST,CUST_NM\n"
            doc = client.post(f"{base}/knowledge", files=dict(file=("s2t.csv", s2t))).json()
            self.assertEqual(doc["mappings"], 1)
            self.assertEqual(client.post(f"{base}/knowledge", files=dict(file=("x.exe", "x"))).status_code, 400)
            r = client.post(f"{base}/match", json=dict(table="customer", column="customer_name")).json()
            self.assertEqual(r["candidates"][0]["source_table"]["name"], "SRC.CRM.CUST")
            self.assertEqual(r["candidates"][0]["source_column"]["name"], "CUST_NM")
            self.assertEqual(r["target"]["table"]["platform"], "DATABRICKS")
            bulk = client.post(f"{base}/bulk", files=dict(file=("list.csv", "table,column\ngold.customer,\nmissing,x\n"))).json()
            self.assertEqual(len(bulk["rows"]), 2)
            self.assertEqual(len(bulk["errors"]), 1)
            exported = client.get(f"{base}/exports/{bulk['export']}.csv")
            self.assertIn("CUST_NM", exported.text)
            self.assertEqual(client.delete(f"{base}/knowledge/s2t.csv").status_code, 200)
            self.assertEqual(client.delete(base).status_code, 200)
            self.assertEqual(client.get("/api/projects").json(), [])

    def test_excel_metadata_upload(self):
        from openpyxl import Workbook

        book = Workbook()
        sheet = book.active
        sheet.append(["Table Name", "Column Name", "Column Description", "Data Type"])
        sheet.append(["SRC.SALES.ORD", "ORD_AMT", "Order amount", "NUMBER(12,2)"])
        buffer = io.BytesIO()
        book.save(buffer)
        rows, report = normalize_metadata(read_table_file("m.xlsx", buffer.getvalue()))
        self.assertEqual(rows[0]["COLUMN_PHYSICAL_NAME"], "ORD_AMT")
        self.assertEqual(report["with_description"], 100)


if __name__ == "__main__":
    unittest.main()

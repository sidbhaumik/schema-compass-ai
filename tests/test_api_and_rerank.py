import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from schema_compass.api import create_app
from schema_compass.app import build_engine, load_settings
from schema_compass.rerank import ClaudeReranker, RerankResult, build_prompt

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
    def test_match_feedback_and_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = settings(tmp)
            client = TestClient(create_app(s, build_engine(s, use_llm=False)))
            self.assertEqual(client.get("/api/status").json()["target"]["tables"], 10)
            r = client.post("/api/match", json=dict(table="returns", column="refund_amount")).json()
            self.assertEqual(r["candidates"][0]["source_column"]["name"], "RFND_AMT")
            self.assertEqual(client.post("/api/match", json=dict(table="nope")).status_code, 404)
            self.assertEqual(client.post("/api/match", json=dict(table="returns", rerank=True)).status_code, 400)
            top = r["candidates"][0]
            ok = client.post("/api/feedback", json=dict(target_table=r["target"]["table"]["id"], target_column="refund_amount",
                                                        source_table=top["source_table"]["id"], source_column="RFND_AMT",
                                                        decision="approved", reviewer="qa"))
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(len(client.get("/api/feedback").json()), 1)
            csv_text = client.get("/api/map-table.csv", params=dict(table="returns")).text
            self.assertIn("RFND_AMT", csv_text)
            self.assertEqual(client.get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()

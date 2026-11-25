import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from schema_compass.api import create_app
from schema_compass.evaluate import evaluate
from schema_compass.workspace import Workspace, list_samples

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


class SampleTests(unittest.TestCase):
    def test_every_sample_loads_and_meets_quality_floor(self):
        samples = list_samples(SAMPLES)
        self.assertGreaterEqual(len(samples), 4)
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            for manifest in samples:
                with self.subTest(sample=manifest["id"]):
                    project = ws.create_from_sample(SAMPLES / manifest["id"])
                    self.assertTrue(project.summary()["ready"])
                    self.assertTrue(project.meta["examples"])
                    engine = project.engine()
                    summary = evaluate(engine, SAMPLES / manifest["id"] / "gold_mappings.csv")["summary"]
                    self.assertEqual(summary["table"]["top1_accuracy"], 1.0)
                    self.assertGreaterEqual(summary["column"]["top1_accuracy"], 0.95)
                    self.assertEqual(summary["column"]["recall_at_k"], 1.0)
                    for example in manifest["examples"]:  # every suggested search resolves
                        table = engine.target.find_table(example["table"])
                        self.assertTrue(not example["column"] or table.column(example["column"]))

    def test_sample_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(Workspace(tmp), samples_dir=SAMPLES))
            listed = client.get("/api/samples").json()
            self.assertEqual(listed[0]["id"], "retail-acquisition")
            created = client.post("/api/samples/people-analytics/projects").json()
            self.assertTrue(created["ready"])
            self.assertEqual(created["source_report"]["file"], "source_metadata.xlsx")
            archive = zipfile.ZipFile(io.BytesIO(client.get("/api/samples/clinic-network.zip").content))
            self.assertIn("clinic-network/knowledge/s2t_wave1_patient.xlsx", archive.namelist())
            self.assertEqual(client.get("/api/samples/nope.zip").status_code, 404)


if __name__ == "__main__":
    unittest.main()

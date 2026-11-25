import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from schema_compass.api import create_app
from schema_compass.storage import VolumeMirror
from schema_compass.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]


class FakeFiles:
    """In-memory stand-in for the Databricks Files API."""

    def __init__(self):
        self.store = {}

    def upload(self, path, contents, overwrite=None):
        self.store[path] = contents.read()

    def download(self, path):
        import io

        return SimpleNamespace(contents=io.BytesIO(self.store[path]))

    def delete(self, path):
        self.store.pop(path, None)

    def list_directory_contents(self, directory):
        prefix = directory.rstrip("/") + "/"
        children = {p[len(prefix):].split("/")[0] for p in self.store if p.startswith(prefix)}
        if not children:
            raise RuntimeError("Not Found: directory does not exist")
        for name in sorted(children):
            full = prefix + name
            yield SimpleNamespace(path=full, name=name, is_directory=full not in self.store)


def server(home, files):
    mirror = VolumeMirror("/Volumes/main/compass/data", home, client=SimpleNamespace(files=files))
    mirror.restore()
    ws = Workspace(home, sync=lambda p, deleted=False: mirror.remove(p) if deleted else mirror.push(p))
    ws.seed_demo(ROOT / "samples")
    return TestClient(create_app(ws))


class VolumeMirrorTests(unittest.TestCase):
    def test_projects_and_decisions_survive_restart(self):
        files = FakeFiles()
        with tempfile.TemporaryDirectory() as first:
            client = server(first, files)
            pid = client.get("/api/projects").json()[0]["id"]
            r = client.post(f"/api/projects/{pid}/match", json=dict(table="returns", column="refund_amount")).json()
            top = r["candidates"][0]
            client.post(f"/api/projects/{pid}/feedback", json=dict(
                target_table=r["target"]["table"]["id"], target_column="refund_amount",
                source_table=top["source_table"]["id"], source_column="RFND_AMT", decision="approved", reviewer="qa"))
            new = client.post("/api/projects", json=dict(name="Wave 2")).json()["id"]
        self.assertTrue(any(p.endswith("/feedback.db") for p in files.store))
        with tempfile.TemporaryDirectory() as second:  # fresh, empty disk: like an app restart
            client = server(second, files)
            names = [p["name"] for p in client.get("/api/projects").json()]
            self.assertEqual(names, ["Retail acquisition", "Wave 2"])  # demo not re-seeded
            self.assertEqual(len(client.get(f"/api/projects/{pid}/feedback").json()), 1)
            client.delete(f"/api/projects/{new}")
        with tempfile.TemporaryDirectory() as third:
            client = server(third, files)
            self.assertEqual([p["name"] for p in client.get("/api/projects").json()], ["Retail acquisition"])

    def test_rejects_non_volume_path(self):
        with self.assertRaises(ValueError):
            VolumeMirror("/tmp/x", ".", client=SimpleNamespace(files=FakeFiles()))


if __name__ == "__main__":
    unittest.main()

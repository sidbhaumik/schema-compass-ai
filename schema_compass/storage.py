"""Durable project storage on a Unity Catalog volume (for Databricks Apps).

Databricks Apps have temporary disks. The workspace keeps working on a local folder for
speed, and this mirror makes it durable: on startup it restores every file from the volume,
and after each change it writes the changed file back through the Files API.
"""
from __future__ import annotations

import io
import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)


class VolumeMirror:
    def __init__(self, volume_path: str, local_root: str | Path, client=None):
        volume_path = volume_path.strip().rstrip("/")
        if volume_path.count(".") == 2 and "/" not in volume_path:  # catalog.schema.volume
            volume_path = "/Volumes/" + volume_path.replace(".", "/")
        if not volume_path.startswith("/Volumes/"):
            raise ValueError(f"Expected a /Volumes/<catalog>/<schema>/<volume> path, got {volume_path!r}")
        self.remote = volume_path + "/schema-compass"
        self.local = Path(local_root).resolve()
        if client is None:
            from databricks.sdk import WorkspaceClient

            client = WorkspaceClient()  # app service principal credentials are injected by Databricks Apps
        self.files = client.files
        self._lock = threading.Lock()

    def _remote_path(self, path: Path) -> str:
        return f"{self.remote}/{path.resolve().relative_to(self.local).as_posix()}"

    def _walk(self, directory: str):
        try:
            entries = list(self.files.list_directory_contents(directory))
        except Exception as error:  # NotFound on first run
            if "not" in str(error).lower() and "found" in str(error).lower() or "does not exist" in str(error).lower():
                return
            raise
        for entry in entries:
            if entry.is_directory:
                yield from self._walk(entry.path.rstrip("/"))
            else:
                yield entry.path

    def restore(self) -> int:
        """Download everything from the volume into the local working folder."""
        count = 0
        for remote in self._walk(self.remote):
            relative = remote[len(self.remote) + 1:]
            target = self.local / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.files.download(remote).contents.read())
            count += 1
        log.info("Restored %d files from %s", count, self.remote)
        return count

    def push(self, path: Path):
        """Write one local file (or every file under a folder) back to the volume."""
        path = Path(path)
        targets = [p for p in path.rglob("*") if p.is_file()] if path.is_dir() else [path]
        with self._lock:
            for item in targets:
                if item.exists():
                    self.files.upload(self._remote_path(item), io.BytesIO(item.read_bytes()), overwrite=True)

    def remove(self, path: Path):
        try:
            self.files.delete(self._remote_path(Path(path)))
        except Exception as error:
            log.warning("Could not delete %s from volume: %s", path, error)

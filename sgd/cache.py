"""Small, resilient file caches used by the addon.

The original implementation stored caches directly in ``/tmp`` and only handled
``FileNotFoundError``.  A truncated pickle / JSON file (very common when two
serverless workers write at the same time) used to crash the whole request.
All read errors are now treated as a cache miss and writes are atomic.
"""

import json
import os
import pickle
import tempfile
import threading
import uuid


try:
    CACHE_DIR = os.path.join(tempfile.gettempdir(), "gdrive_stremio_cache")
    os.makedirs(CACHE_DIR, exist_ok=True)
except OSError:  # pragma: no cover - extremely restricted environments
    CACHE_DIR = tempfile.gettempdir()

_LOCK = threading.RLock()


class Cache:
    def __init__(self, filename, filetype):
        self.filetype = filetype
        self.bin = "b" if filetype is pickle else ""
        self.filename = os.path.join(CACHE_DIR, os.path.basename(filename))
        self.contents = {}
        self.load()

    def load(self):
        """Load the cache, silently resetting it when it is missing/corrupt."""
        with _LOCK:
            try:
                with open(self.filename, "r" + self.bin) as file_:
                    contents = self.filetype.load(file_)
                self.contents = contents if isinstance(contents, dict) else {}
            except Exception as exc:  # corrupt pickle/json, permissions, etc.
                self.contents = {}
                self.save("Reset unreadable cache {}: {}".format(
                    os.path.basename(self.filename), exc
                ))

    def save(self, mess="Saving"):
        """Write through a temporary file so readers never see partial data."""
        with _LOCK:
            temp_name = "{}.{}.tmp".format(self.filename, uuid.uuid4().hex)
            try:
                with open(temp_name, "w" + self.bin) as file_:
                    self.filetype.dump(self.contents, file_)
                os.replace(temp_name, self.filename)
            except Exception as exc:
                print("WARNING: could not write cache {}: {}".format(self.filename, exc))
                try:
                    os.remove(temp_name)
                except OSError:
                    pass


class Pickle(Cache):
    def __init__(self, filename):
        super().__init__(filename, pickle)


class Json(Cache):
    def __init__(self, filename):
        super().__init__(filename, json)

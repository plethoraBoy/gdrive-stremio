"""Flask application bootstrap."""

import json
import logging
import os

from flask import Flask

from sgd.gdrive import GoogleDrive


app = Flask(__name__)


def _load_token():
    raw = os.environ.get("TOKEN")
    if not raw:
        print("WARNING: TOKEN environment variable is not set.")
        return None
    try:
        token = json.loads(raw)
    except (TypeError, ValueError) as exc:
        print("ERROR: TOKEN is not valid JSON: {}".format(exc))
        return None
    if not isinstance(token, dict):
        print("ERROR: TOKEN must be a JSON object.")
        return None
    return token


token = _load_token()
gdrive = None
if token:
    try:
        gdrive = GoogleDrive(token)
    except Exception as exc:
        # Keep the addon alive so /manifest.json and health checks still work;
        # stream requests will return an empty list with a clear log entry.
        logging.exception("Could not initialise Google Drive: %s", exc)

# Imported last because the routes need ``app`` and ``gdrive`` to exist.
from sgd import routes  # noqa: E402,F401

"""HTTP routes exposed to Stremio."""

import traceback
from datetime import datetime
from json import dumps

from flask import abort, Response, jsonify

from sgd import app, gdrive
from sgd.meta import Meta, MetadataNotFound
from sgd.streams import Streams


MANIFEST = {
    "id": "shuvamjaswal.stremio.googledrive",
    "version": "1.2.0",
    "name": "GDrive",
    "description": "Stream videos from your Google Drive in Stremio.",
    "logo": "https://fonts.gstatic.com/s/i/productlogos/drive_2020q4/v8/web-512dp/logo_drive_2020q4_color_1x_web_512dp.png",
    "resources": ["stream"],
    "types": ["movie", "series"],
    "catalogs": [],
}


def common_headers(resp_obj):
    resp_obj.headers["Access-Control-Allow-Origin"] = "*"
    resp_obj.headers["Access-Control-Allow-Headers"] = "*"
    resp_obj.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp_obj.headers["X-Robots-Tag"] = "noindex"
    return resp_obj


@app.route("/")
def init():
    return "Addon is alive."


@app.route("/health")
def health():
    return common_headers(jsonify({"status": "ok", "configured": gdrive is not None}))


@app.route("/manifest.json")
def addon_manifest():
    return common_headers(jsonify(MANIFEST))


@app.route("/stream/<stream_type>/<stream_id>.json")
@app.route("/stream/<stream_type>/<stream_id>")
def addon_stream(stream_type, stream_id):
    if stream_type not in MANIFEST["types"] or not str(stream_id).lower().startswith("tt"):
        abort(404)

    # Streaming the body lets long Drive searches outlive the platform's
    # short first-byte timeout.
    response = Response(get_streams(stream_type, stream_id), mimetype="application/json")
    return common_headers(response)


def get_streams(stream_type, stream_id):
    yield '{"streams":'
    payload = "[]"

    try:
        start_time = datetime.now()
        time_taken = lambda st: "{:.3f}s".format((datetime.now() - st).total_seconds())

        if gdrive is None:
            raise RuntimeError(
                "Addon is not configured: set the TOKEN environment variable."
            )

        stream_meta = Meta(stream_type, stream_id)
        gdrive.search(stream_meta)
        print(
            "Got {}/{} unique results from gdrive after deduping in {}.".format(
                len(gdrive.results), gdrive.len_response, time_taken(start_time)
            )
            + " Processing results..."
        )

        streams = Streams(gdrive, stream_meta)
        print(
            "Fetched {}/{} valid stream(s) in {} for {} -> {}".format(
                len(streams.results),
                len(gdrive.results),
                time_taken(start_time),
                stream_id,
                gdrive.query,
            )
        )
        payload = dumps(streams.results)
    except MetadataNotFound as exc:
        print("No metadata found: {}".format(exc))
    except Exception as exc:
        # Never break the JSON contract with Stremio: an error becomes an
        # empty stream list instead of a truncated / invalid response.
        print("ERROR while building streams: {}".format(exc))
        traceback.print_exc()

    yield payload + "}"

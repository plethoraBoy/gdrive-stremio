"""Turn raw Drive file metadata into Stremio stream objects."""

import logging
import os
import re
import urllib.parse

from sgd.ptn import parse_title
from sgd.utils import hr_size, sanitize


logger = logging.getLogger(__name__)


def _int_list(value):
    """Normalise PTN's int / str / list season and episode values."""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        result = []
        for item in value:
            result.extend(_int_list(item))
        return result
    try:
        return [int(value)]
    except (TypeError, ValueError):
        return []


class Streams:
    def __init__(self, gdrive, stream_meta):
        self.results = []
        self.gdrive = gdrive
        self.strm_meta = stream_meta
        self.proxy_url = (os.environ.get("CF_PROXY_URL") or "").strip().rstrip("/")
        self.get_url = self.get_proxy_url if self.proxy_url else self.get_gapi_url
        self.acc_token = None

        if not self.proxy_url:
            try:
                self.acc_token = gdrive.get_acc_token()
            except Exception as exc:
                logger.warning("Could not get an access token: %s", exc)

        for item in gdrive.results or []:
            try:
                self.item = item
                self.parsed = parse_title(str(item.get("name") or ""))
                self.construct_stream()
                if not self.is_semi_valid_title(self.constructed):
                    continue
                if self.strm_meta.type != "movie" or self.is_valid_year(self.constructed):
                    self.results.append(self.constructed)
            except Exception as exc:
                logger.warning("Skipping Drive item %s: %s", item.get("id"), exc)

        try:
            self.results.sort(key=self.best_res, reverse=True)
        except Exception as exc:
            logger.warning("Could not sort streams: %s", exc)

    def is_valid_year(self, movie):
        expected = str(getattr(self.strm_meta, "year", "") or "").strip()
        if not expected:
            return True
        actual = str((movie.get("sortkeys") or {}).get("year") or "").strip()
        try:
            return abs(int(actual) - int(expected)) <= 1
        except (TypeError, ValueError):
            return expected in actual

    def is_semi_valid_title(self, item):
        item_title = sanitize((item.get("sortkeys") or {}).get("title"), "")
        if not item_title:
            return False
        for title in getattr(self.strm_meta, "titles", None) or []:
            wanted = sanitize(title, "")
            if wanted and wanted in item_title:
                return True
        return False

    def get_title(self):
        file_name = str(self.item.get("name") or "video")
        file_size = hr_size(self.item.get("size"))
        drive_id = self.item.get("driveId")
        drive_name = self.gdrive.drive_names.contents.get(drive_id) if drive_id else None
        drive_name = str(drive_name or "MyDrive")
        if len(drive_name) > 100:
            drive_name = drive_name[:97] + "..."

        str_format = "🎥;%codec 🌈;%bitDepth;bit 🔊;%audio 👤;%encoder"
        suffix = self.parsed.get_str(str_format)
        return "{}\n💾 {} ☁️ {}\n{}".format(file_name, file_size, drive_name, suffix)

    def get_proxy_url(self):
        file_id = urllib.parse.quote(str(self.item.get("id") or ""), safe="")
        file_name = urllib.parse.quote(
            str(self.item.get("name") or "file_name.vid"), safe=""
        )
        self.constructed["behaviorHints"]["proxyHeaders"] = {
            "request": {"Server": "Stremio"}
        }
        return "{}/load/{}/{}".format(self.proxy_url, file_id, file_name)

    def get_gapi_url(self):
        file_id = urllib.parse.quote(str(self.item.get("id") or ""), safe="")
        file_name = urllib.parse.quote(
            str(self.item.get("name") or "file_name.vid"), safe=""
        )
        if self.acc_token:
            self.constructed["behaviorHints"]["proxyHeaders"] = {
                "request": {"Authorization": "Bearer {}".format(self.acc_token)}
            }
        return (
            "https://www.googleapis.com/drive/v3/files/{}"
            "?alt=media&file_name={}".format(file_id, file_name)
        )

    def construct_stream(self):
        self.constructed = {"behaviorHints": {"notWebReady": True}}
        resolution = self.parsed.sortkeys.get("res") or "1"
        self.constructed["behaviorHints"]["bingeGroup"] = "gdrive-{}".format(resolution)

        self.constructed["url"] = self.get_url()
        self.constructed["name"] = self.parsed.get_str("GDrive %resolution %quality")
        self.constructed["title"] = self.get_title()
        self.constructed["sortkeys"] = dict(self.parsed.sortkeys)
        return self.constructed

    def best_res(self, item):
        sortkeys = item.get("sortkeys") or {}
        resolution = sortkeys.get("res")

        res_map = {
            "hd": 720,
            "sd": 480,
            "1280x720": 720,
            "1280x720p": 720,
            "1920x1080": 1080,
            "fhd": 1080,
            "uhd": 2160,
            "4k": 2160,
        }
        key = str(resolution or "").lower()
        digits = re.search(r"(\d{3,4})", key)
        try:
            sort_int = int(digits.group(1)) if digits else res_map.get(key, 1)
        except (TypeError, ValueError):
            sort_int = 1

        ptn_name = sanitize(sortkeys.get("title"), "")
        name_match = any(
            ptn_name.endswith(sanitize(title, ""))
            for title in (getattr(self.strm_meta, "titles", None) or [])
            if sanitize(title, "")
        )
        if not name_match:
            sort_int -= 2160

        if self.strm_meta.type == "series":
            se_list = _int_list(sortkeys.get("se"))
            ep_list = _int_list(sortkeys.get("ep"))
            wanted_se = _int_list(getattr(self.strm_meta, "se", None))
            wanted_ep = _int_list(getattr(self.strm_meta, "ep", None))
            if not se_list or (wanted_se and wanted_se[0] not in se_list):
                sort_int -= 2160 * 2
            if not ep_list or (wanted_ep and wanted_ep[0] not in ep_list):
                sort_int -= 2160 * 2

        return sort_int

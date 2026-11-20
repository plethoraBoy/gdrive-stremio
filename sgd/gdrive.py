"""Google Drive search with retrying, pagination and safe query building."""

import logging
import os
import threading
import time

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from sgd.cache import Json, Pickle


logger = logging.getLogger(__name__)

TOKEN_URI = "https://oauth2.googleapis.com/token"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
MAX_RETRIES = 3
MAX_PAGES = 10
MAX_ROUNDS = MAX_PAGES * (MAX_RETRIES + 1)


def _escape(value):
    """Escape a value for a Google Drive ``contains '...'`` clause."""
    return str(value if value is not None else "").replace("\\", "\\\\").replace("'", "\\'")


def _safe_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class GoogleDrive:
    def __init__(self, token):
        if not isinstance(token, dict) or not token.get("refresh_token"):
            raise ValueError(
                "TOKEN is missing or invalid. Set the TOKEN environment variable "
                "to the JSON credentials object produced by the Colab notebook."
            )

        self.token = token
        self.page_size = 1000
        self.max_titles = _safe_int(os.environ.get("MAX_TITLES"), 60) or 60
        self.acc_token = Pickle("acctoken.pickle")
        self.drive_names = Json("drivenames.json")
        self.results = []
        self.query = []
        self.len_response = 0
        self._creds_lock = threading.RLock()

        self.creds = self._build_credentials()
        self.drive_instance = self._build_service()

    # ------------------------------------------------------------------ auth
    def _build_credentials(self):
        info = dict(self.token)
        info.setdefault("token_uri", TOKEN_URI)
        try:
            creds = Credentials.from_authorized_user_info(info, scopes=[DRIVE_SCOPE])
        except Exception:
            creds = Credentials(
                token=info.get("access_token"),
                refresh_token=info.get("refresh_token"),
                token_uri=info.get("token_uri", TOKEN_URI),
                client_id=info.get("client_id"),
                client_secret=info.get("client_secret"),
                scopes=[DRIVE_SCOPE],
            )

        # A token dict without 'expiry' makes google-auth consider the access
        # token valid forever, which leads to intermittent 401s.  Refresh now.
        if creds.refresh_token and (not info.get("expiry") or creds.expired or not creds.token):
            try:
                creds.refresh(GoogleAuthRequest())
            except Exception as exc:
                logger.warning("Initial OAuth refresh failed: %s", exc)
        return creds

    def _build_service(self):
        # cache_discovery=False avoids writing to a read-only home directory on
        # serverless platforms.
        return build("drive", "v3", credentials=self.creds, cache_discovery=False)

    def _refresh_credentials(self):
        with self._creds_lock:
            try:
                self.creds.refresh(GoogleAuthRequest())
            except Exception as exc:
                logger.warning("OAuth refresh failed: %s", exc)
                data = self._fetch_access_token()
                if data and self.creds:
                    self.creds.token = data["access_token"]
                    self.creds.expiry = time.time() + int(data.get("expires_in", 3600))
            self.drive_instance = self._build_service()

    def _fetch_access_token(self):
        body = {
            "client_id": self.token.get("client_id", ""),
            "client_secret": self.token.get("client_secret", ""),
            "refresh_token": self.token.get("refresh_token", ""),
            "grant_type": "refresh_token",
        }
        try:
            response = requests.post(TOKEN_URI, json=body, timeout=10)
            data = response.json()
        except Exception as exc:
            logger.error("Access token request failed: %s", exc)
            return None

        if not isinstance(data, dict) or not data.get("access_token"):
            logger.error("Access token request rejected: %s", data)
            return None

        data["expires_at"] = time.time() + _safe_int(data.get("expires_in"), 3600)
        self.acc_token.contents = data
        self.acc_token.save()
        return data

    def get_acc_token(self):
        """Return a valid access token for direct (proxy-less) streaming."""
        cached = self.acc_token.contents
        if isinstance(cached, dict):
            expiry = cached.get("expires_at")
            if expiry is None and hasattr(cached.get("expires_in"), "timestamp"):
                # Backwards compatibility with the old datetime-based cache.
                expiry = cached["expires_in"].timestamp()
            token = cached.get("access_token")
            if token and expiry and float(expiry) > time.time() + 60:
                return token

        data = self._fetch_access_token()
        if data:
            return data["access_token"]
        return getattr(self.creds, "token", None)

    # --------------------------------------------------------------- queries
    @staticmethod
    def qgen(string, chain="and", splitter=" ", method=None):
        words = [word for word in str(string or "").split(splitter) if word]
        if not words:
            return ""

        clauses = []
        for word in words:
            field = method or ("fullText" if word.isdigit() else "name")
            clauses.append("{} contains '{}'".format(field, _escape(word)))
        return (" {} ".format(chain)).join(clauses)

    @staticmethod
    def _title_query(title):
        words = [word for word in str(title or "").split() if word]
        if not words:
            return ""
        if len(words) == 1:
            word = _escape(words[0])
            return "fullText contains '\"{}\"' and name contains '{}'".format(word, word)
        return GoogleDrive.qgen(title)

    def get_query(self, sm):
        out = []
        seen = set()

        titles = []
        for title in (getattr(sm, "titles", None) or []):
            title = str(title or "").strip()
            if title and title not in seen:
                seen.add(title)
                titles.append(title)
        titles = titles[: self.max_titles]

        if sm.stream_type == "series":
            se, ep = _safe_int(sm.se), _safe_int(sm.ep)
            seep_q = ""
            if se is not None and ep is not None:
                variants = [
                    "s{:02d} e{:02d}".format(se, ep),
                    "s{} e{}".format(se, ep),
                    "season {} episode {}".format(se, ep),
                    "\"{} x {:02d}\"".format(se, ep),
                    "\"{} x {}\"".format(se, ep),
                ]
                seep_q = self.qgen(
                    ", ".join(variants), chain="or", splitter=", ", method="fullText"
                )

            for title in titles:
                title_q = self._title_query(title)
                if not title_q:
                    continue
                out.append(
                    "({}) and ({})".format(title_q, seep_q) if seep_q else title_q
                )
        else:
            year = str(getattr(sm, "year", "") or "").strip()
            for title in titles:
                title_q = self._title_query(title)
                if not title_q:
                    continue
                out.append(
                    "{} and fullText contains '{}'".format(title_q, _escape(year))
                    if year
                    else title_q
                )

        return list(dict.fromkeys(out))

    # ------------------------------------------------------------ drive calls
    @staticmethod
    def _is_auth_error(exception):
        status = getattr(getattr(exception, "resp", None), "status", None)
        if status in (401, 403):
            return True
        message = str(exception).lower()
        return "invalid_grant" in message or "token expired" in message

    def file_list(self, file_fields):
        """Run all queries as batches, with retries and result pagination."""
        if not self.query:
            return []

        base_q = "trashed=false and mimeType contains 'video/'"
        pending = [(index, query, None, 0) for index, query in enumerate(self.query)]
        output = []
        seen_ids = set()

        for _ in range(MAX_ROUNDS):
            if not pending:
                break

            request_map = {}
            failed = []
            next_pages = []

            def callback(request_id, response, exception):
                entry = request_map.get(request_id)
                if exception is not None:
                    failed.append((entry, exception))
                    return
                if not isinstance(response, dict):
                    return
                for item in response.get("files") or []:
                    file_id = item.get("id")
                    if file_id and file_id not in seen_ids:
                        seen_ids.add(file_id)
                        output.append(item)
                page_token = response.get("nextPageToken")
                if page_token and entry:
                    next_pages.append((entry[0], entry[1], page_token, 0))

            batch = self.drive_instance.new_batch_http_request()
            for index, query, page_token, attempt in pending:
                request_id = "{}|{}".format(index, page_token or "")
                request_map[request_id] = (index, query, page_token, attempt)
                params = {
                    "q": "({}) and {}".format(query, base_q),
                    "fields": "nextPageToken,files({})".format(file_fields),
                    "pageSize": self.page_size,
                    "supportsAllDrives": True,
                    "includeItemsFromAllDrives": True,
                    "corpora": "allDrives",
                }
                if page_token:
                    params["pageToken"] = page_token
                batch.add(
                    self.drive_instance.files().list(**params),
                    callback=callback,
                    request_id=request_id,
                )

            try:
                batch.execute()
            except Exception as exc:
                logger.error("Drive batch request failed: %s", exc)
                failed.append((None, exc))

            if any(self._is_auth_error(exc) for _, exc in failed):
                self._refresh_credentials()

            retry = []
            for entry, exception in failed:
                if entry is None:
                    # The whole batch failed; retry everything in it.
                    retry.extend(pending)
                    break
                if entry[3] < MAX_RETRIES:
                    retry.append((entry[0], entry[1], entry[2], entry[3] + 1))
                else:
                    logger.error("Giving up on Drive query after retries: %s", entry[1])

            pending = retry + next_pages
            if retry:
                time.sleep(min(2 ** min(len(retry), 4), 8))

        return output

    def get_drive_names(self):
        def callback(request_id, response, exception):
            if exception is None and isinstance(response, dict):
                if response.get("name"):
                    self.drive_names.contents[request_id] = response["name"]

        drive_ids = {
            item.get("driveId")
            for item in self.results
            if isinstance(item, dict) and item.get("driveId")
        }
        missing = [drive_id for drive_id in drive_ids if not self.drive_names.contents.get(drive_id)]
        if not missing:
            return self.drive_names.contents

        batch = self.drive_instance.new_batch_http_request()
        for drive_id in missing:
            self.drive_names.contents[drive_id] = None
            batch.add(
                self.drive_instance.drives().get(driveId=drive_id, fields="id,name"),
                callback=callback,
                request_id=drive_id,
            )
        try:
            batch.execute()
        except Exception as exc:
            logger.warning("Could not fetch drive names: %s", exc)

        # Remove unresolved entries so the next search can try again.
        for drive_id in list(self.drive_names.contents):
            if self.drive_names.contents[drive_id] is None:
                del self.drive_names.contents[drive_id]
        self.drive_names.save()
        return self.drive_names.contents

    # ----------------------------------------------------------------- search
    def search(self, stream_meta):
        self.results = []
        self.len_response = 0
        self.query = []

        try:
            self.query = self.get_query(stream_meta)
        except Exception as exc:
            logger.exception("Could not build Drive query: %s", exc)
            return self.results

        try:
            response = self.file_list("id,name,size,driveId,md5Checksum")
        except Exception as exc:
            logger.exception("Drive search failed: %s", exc)
            response = []

        self.len_response = len(response)
        uids = set()

        def is_unique(item):
            if not isinstance(item, dict) or not item.get("id"):
                return False
            key = (
                item.get("driveId") or "MyDrive",
                item.get("md5Checksum") or item["id"],
            )
            if key in uids:
                return False
            uids.add(key)
            return True

        def size_of(item):
            return _safe_int(item.get("size"), 0) or 0

        self.results = sorted(
            filter(is_unique, response), key=size_of, reverse=True
        )

        try:
            self.get_drive_names()
        except Exception as exc:
            logger.warning("Drive name lookup failed: %s", exc)

        return self.results

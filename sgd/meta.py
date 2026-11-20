"""Metadata lookup with graceful fallbacks.

IMDb's HTML is tried first (it gives us localised/aka titles), then the IMDb
suggest API, then Cinemeta.  Every step is defensive: one malformed page must
never take down the whole addon.
"""

import re

import sgd.utils as ut
from bs4 import BeautifulSoup
from sgd.cache import Json


try:  # lxml is faster, but html.parser keeps the addon working without it.
    import lxml  # noqa: F401
    HTML_PARSER = "lxml"
except ImportError:
    HTML_PARSER = "html.parser"


class MetadataNotFound(Exception):
    pass


class IMDb:
    def __init__(self):
        self.titles = getattr(self, "titles", []) or []
        self.year = getattr(self, "year", None)

        self.imdb_sg_url = "v2.sg.media-imdb.com/suggests/{}/{}.json".format(
            (self.id or "t")[0], self.id
        )
        self.cinemeta_url = "v3-cinemeta.strem.io/meta/{}/{}.json".format(
            self.type, self.id
        )
        self.imdb_html_url = "imdb.com/title/{}/releaseinfo?ref_=tt_dt_aka".format(
            self.id
        )

        self.fetch_dest = "IMDB_HTML"
        if not self.get_meta_from_imdb_html():
            self.fetch_dest = "IMDB_SG_API"
            if not self.get_meta_from_imdb_sg():
                self.fetch_dest = "CINEMETA"
                if not self.get_meta_from_cinemeta():
                    self.fetch_dest = "NULL"
                    raise MetadataNotFound(
                        "Couldn't find metadata for {} {}!".format(self.type, self.id)
                    )

    def get_meta_from_imdb_html(self):
        """Scrape the IMDb aka page, including local titles, when available."""
        try:
            imdb_html = ut.req_wrapper(self.imdb_html_url, time_out=5)
            if not imdb_html:
                return False
            soup = BeautifulSoup(imdb_html, HTML_PARSER)
            table = soup.find("table", attrs={"class": "akas-table-test-only"})
            r_title_block = soup.find(
                "div", attrs={"class": "subpage_title_block__right-column"}
            )

            if r_title_block:
                h3_itemprop = r_title_block.find("h3", attrs={"itemprop": "name"})
                h4_itemprop = r_title_block.find("h4", attrs={"itemprop": "name"})

                title = ""
                if h4_itemprop and h4_itemprop.find("a"):
                    # Only appears on some pages like tt1672218
                    title = ut.sanitize(h4_itemprop.find("a").text) + " "

                if h3_itemprop:
                    h3_link = h3_itemprop.find("a")
                    if h3_link and h3_link.text:
                        title += ut.sanitize(h3_link.text)
                        if title.strip():
                            self.titles.append(title.strip())

                    span = h3_itemprop.find("span")
                    if span:
                        years = list(filter(ut.is_year, ut.num_extract(span.text)))
                        self.year = min(years) if years else self.year

            if table:
                titles = set()
                first_title = ut.safe_get(self.titles, 0)
                for tr in table.find_all("tr"):
                    cells = tr.find_all("td")
                    cell = ut.safe_get(cells, 1)
                    if not hasattr(cell, "text"):
                        continue
                    title = ut.sanitize(cell.text)
                    if title and title != first_title and not (
                        title.isdigit() and len(title) < 3
                    ):
                        titles.add(title)

                # Keep the batch size sane for the Drive API.
                self.titles += list(titles)[:60]

            self.titles = list(dict.fromkeys(t for t in self.titles if t))
            if self.titles and (self.type == "series" or self.year):
                return True
        except Exception as exc:
            print("IMDb HTML lookup failed: {}".format(exc))
        return False

    def get_meta_from_imdb_sg(self):
        """Obtain metadata from the IMDb suggestions API."""
        try:
            meta = ut.req_api(self.imdb_sg_url, key="d")
            if isinstance(meta, list) and meta:
                return self.set_meta(meta[0], year="y", title="l")
        except Exception as exc:
            print("IMDb suggestion lookup failed: {}".format(exc))
        return False

    def get_meta_from_cinemeta(self):
        """Obtain metadata from the Cinemeta v3 API."""
        try:
            data = ut.req_api(self.cinemeta_url, key=None)
            meta = data.get("meta") if isinstance(data, dict) else data
            if isinstance(meta, dict):
                return self.set_meta(meta)
        except Exception as exc:
            print("Cinemeta lookup failed: {}".format(exc))
        return False

    def set_meta(self, meta, year="year", title="name"):
        try:
            name = ut.sanitize(meta.get(title, ""))
            if not name:
                return False
            self.titles.append(name)

            raw_year = str(meta.get(year, "") or "")
            match = re.search(r"(?:18|19|20)\d{2}", raw_year)
            self.year = match.group(0) if match else None
            return True
        except (AttributeError, TypeError):
            return False


class Meta(IMDb):
    def __init__(self, stream_type, stream_id):
        self.titles = []
        self.year = None
        self.ep = "01"
        self.se = "01"
        self.fetch_dest = "UNKNOWN"

        # Stremio may send ':' literally or percent-encoded as %3A.  WSGI
        # servers usually decode it, so we have to accept both forms.
        parts = str(stream_id or "").replace("%3A", ":").split(":")
        self.id = parts[0]
        self.type = stream_type
        self.stream_type = stream_type

        if stream_type == "series":
            numbers = [p for p in parts[1:] if str(p).isdigit()]
            if len(numbers) >= 2:
                season, episode = numbers[-2], numbers[-1]
            elif numbers:
                season, episode = "1", numbers[0]
            else:
                season, episode = "1", "1"
            self.se = str(int(season)).zfill(2)
            self.ep = str(int(episode)).zfill(2)

        cache = Json("meta_{}.json".format(self.id))
        cached = cache.contents
        valid_cache = (
            isinstance(cached, dict)
            and isinstance(cached.get("titles"), list)
            and bool(cached["titles"])
        )

        if valid_cache:
            # Refresh se/ep so a series never reuses the last cached episode.
            cached["se"], cached["ep"] = self.se, self.ep
            self.__dict__.update(cached)
            self.fetch_dest = "CACHE"
        else:
            IMDb.__init__(self)
            cache.contents = dict(self.__dict__)
            cache.save()

        if not self.titles:
            raise MetadataNotFound(
                "Couldn't find metadata for {} {}!".format(self.type, self.id)
            )

        print("Fetched metadata for {} from {}:".format(self.id, self.fetch_dest))
        print("Titles ({}) -> {}".format(len(self.titles), self.titles))
        print("Year: {}".format(self.year))

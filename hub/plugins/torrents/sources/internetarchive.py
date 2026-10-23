"""Internet Archive: public-domain and freely licensed items that ship an official torrent.

Documented API: https://archive.org/advancedsearch.php (no key needed). Every item with the
format "Archive BitTorrent" has ``/download/<id>/<id>_archive.torrent``. The Archive doesn't
publish seeder counts through this API, so seeders/leechers stay ``None``.
"""

from __future__ import annotations

import re

from ..model import Provider, ProviderError, Result, fetch_json

API = "https://archive.org/advancedsearch.php"
LUCENE = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]')


class InternetArchive(Provider):
    id = "internetarchive"
    name = "Internet Archive"

    async def search(self, session, query, page, page_size, timeout):
        terms = " ".join(LUCENE.sub(" ", query).split())
        if not terms:
            return []
        params = {"q": '(%s) AND format:("Archive BitTorrent")' % terms, "rows": page_size,
                  "page": page, "output": "json", "sort[]": "downloads desc"}
        params = [(k, v) for k, v in params.items()] + [("fl[]", f) for f in
                                                        ("identifier", "title", "item_size")]
        data = await fetch_json(session, API, params, timeout)
        try:
            docs = data["response"]["docs"]
            if not isinstance(docs, list):
                raise TypeError
        except (KeyError, TypeError) as err:
            raise ProviderError("malformed", "unexpected answer shape") from err
        out = []
        for rank, d in enumerate(docs):
            if not isinstance(d, dict) or not d.get("identifier"):
                continue
            ident = str(d["identifier"])
            title = d.get("title")
            if isinstance(title, list):
                title = title[0] if title else ""
            out.append(Result(
                title=str(title or ident), source=self.name, size=d.get("item_size"),
                url="https://archive.org/download/%s/%s_archive.torrent" % (ident, ident), rank=rank))
        return out


SOURCE = InternetArchive

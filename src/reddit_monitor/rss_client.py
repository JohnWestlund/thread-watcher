"""Credential-free fallback that reads a post's public comment feed (the post URL + ".rss").

Same interface as RedditClient, with less information. Reddit's comment feed is flat:
it doesn't say which comment a reply is answering, and it has no scores or author
details. So in this mode:
  - parent_id is "unknown"; every comment is treated as possibly addressed to the owner
  - "you already replied" can't be detected automatically, so mark those by hand
  - there are no score, account-age or karma signals
Use it while waiting for API approval; switch to the API client once you have credentials.
"""

from __future__ import annotations

import html
import os
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from calendar import timegm
from html.parser import HTMLParser

ATOM = "{http://www.w3.org/2005/Atom}"
UNKNOWN_PARENT = "unknown"
DEFAULT_UA = "python:thread-watcher:0.1 (personal read-only comment monitor)"


class _TextExtractor(HTMLParser):
    BREAKS = {"p", "br", "li", "blockquote", "pre", "div", "tr"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.quote_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "blockquote":
            self.quote_depth += 1
        if tag in self.BREAKS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag == "blockquote":
            self.quote_depth = max(0, self.quote_depth - 1)
        if tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.quote_depth:
            data = "> " + data
        self.parts.append(data)

    def text(self) -> str:
        t = "".join(self.parts)
        return re.sub(r"\n{3,}", "\n\n", t).strip()


def html_to_text(fragment: str) -> str:
    p = _TextExtractor()
    p.feed(fragment or "")
    return p.text()


def _ts(value: str | None) -> float | None:
    if not value:
        return None
    value = re.sub(r"\.\d+", "", value).replace("Z", "+00:00")
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)([+-]\d\d):?(\d\d)", value)
    if not m:
        return None
    base = timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"))
    sign = 1 if m.group(2)[0] == "+" else -1
    offset = sign * (abs(int(m.group(2))) * 3600 + int(m.group(3)) * 60)
    return base - offset


def _post_url(url_or_id: str) -> str:
    if re.match(r"^https?://", url_or_id):
        url = url_or_id.split("?")[0].split("#")[0]
        url = re.sub(r"^https?://(old\.|new\.|np\.|m\.)?reddit\.com", "https://www.reddit.com", url)
        url = re.sub(r"\.(rss|json)$", "", url)
        return url.rstrip("/") + "/"
    return f"https://www.reddit.com/comments/{url_or_id.removeprefix('t3_')}/"


def parse_feed(xml_text: str) -> tuple[dict | None, list[dict]]:
    """Return (post, comments) from a post's comment feed."""
    root = ET.fromstring(xml_text)
    post, comments = None, []
    for e in root.findall(f"{ATOM}entry"):
        raw_id = (e.findtext(f"{ATOM}id") or "").strip()
        author = (e.findtext(f"{ATOM}author/{ATOM}name") or "").strip().removeprefix("/u/").removeprefix("u/")
        link_el = e.find(f"{ATOM}link")
        link = link_el.get("href") if link_el is not None else ""
        body = html_to_text(html.unescape(e.findtext(f"{ATOM}content") or ""))
        created = _ts(e.findtext(f"{ATOM}published")) or _ts(e.findtext(f"{ATOM}updated"))
        cat = e.find(f"{ATOM}category")
        if raw_id.startswith("t3_"):
            body = re.sub(r"\s*submitted by\s+/u/\S+.*$", "", body, flags=re.S).strip()
            post = {
                "id": raw_id[3:],
                "title": (e.findtext(f"{ATOM}title") or "").strip(),
                "subreddit": cat.get("term") if cat is not None else "",
                "author": author or "[deleted]",
                "selftext": body,
                "url": link,
                "created_utc": created,
                "score": None,
                "locked": None,
            }
        elif raw_id.startswith("t1_"):
            comments.append({
                "id": raw_id[3:],
                "parent_id": UNKNOWN_PARENT,
                "author": author or "[deleted]",
                "body": body,
                "created_utc": created,
                "score": None,
                "permalink": link,
                "is_submitter": None,
                "distinguished": None,
                "edited": False,
                "removed": body in ("[removed]", "[deleted]"),
                "depth": None,
            })
    return post, comments


class RSSClient:
    supports_author_info = False
    supports_threading = False

    CACHE_SECONDS = 30  # watch -> check -> context in quick succession shares one fetch

    def __init__(self, user_agent: str | None = None, fetch=None, min_interval: float = 2.0):
        self.user_agent = user_agent or os.environ.get("REDDIT_USER_AGENT") or DEFAULT_UA
        self._fetch = fetch or self._http_get
        self.min_interval = min_interval
        self._last_request = 0.0
        self._cache: dict[str, tuple[float, str]] = {}

    def _get(self, url: str) -> str:
        hit = self._cache.get(url)
        if hit and time.time() - hit[0] < self.CACHE_SECONDS:
            return hit[1]
        text = self._fetch(url)
        self._cache[url] = (time.time(), text)
        return text

    def _http_get(self, url: str, retry: bool = True) -> str:
        wait = self.min_interval - (time.time() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 429 and retry:
                delay = min(30.0, float(e.headers.get("Retry-After") or 10))
                time.sleep(delay)
                return self._http_get(url, retry=False)
            if e.code == 429:
                raise RuntimeError("Reddit is rate limiting the feed (HTTP 429). Check less often.") from e
            if e.code in (403, 404):
                raise RuntimeError(f"Reddit returned HTTP {e.code} for {url}. The post may be private, "
                                   "removed, or this network may be blocked.") from e
            raise
        finally:
            self._last_request = time.time()

    def _feed(self, url_or_id: str) -> tuple[dict, list[dict]]:
        url = _post_url(url_or_id) + ".rss?sort=new&limit=100"
        post, comments = parse_feed(self._get(url))
        if post is None:
            raise RuntimeError(f"No post found in the feed for {url_or_id}.")
        post["num_comments"] = len(comments)
        return post, comments

    def get_post(self, url_or_id: str) -> dict:
        return self._feed(url_or_id)[0]

    def get_comments(self, post_id: str) -> list[dict]:
        return self._feed(post_id)[1]

    def get_author(self, name: str) -> dict:
        return {"name": name}

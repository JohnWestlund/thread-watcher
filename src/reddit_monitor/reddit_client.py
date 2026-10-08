"""Read-only access to Reddit through PRAW.

Uses application-only OAuth (client id + secret, no username/password), so the
credentials it runs with cannot post, vote, or edit anything. Everything is
converted to plain dicts so the rest of the code (and tests) never touch PRAW.
"""

from __future__ import annotations

import os
import re

DELETED = "[deleted]"


def _author_name(thing) -> str:
    return thing.author.name if getattr(thing, "author", None) else DELETED


class RedditClient:
    def __init__(self, client_id: str | None = None, client_secret: str | None = None,
                 user_agent: str | None = None, replace_more_limit: int | None = None):
        import praw  # imported lazily so tests don't need credentials

        client_id = client_id or os.environ.get("REDDIT_CLIENT_ID")
        client_secret = client_secret or os.environ.get("REDDIT_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise RuntimeError(
                "REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET must be set "
                "(create a 'script' app at https://www.reddit.com/prefs/apps)."
            )
        user_agent = user_agent or os.environ.get("REDDIT_USER_AGENT") or "reddit-monitor-mcp/0.1 (read-only)"
        self.reddit = praw.Reddit(client_id=client_id, client_secret=client_secret, user_agent=user_agent)
        self.reddit.read_only = True
        if replace_more_limit is None:
            replace_more_limit = int(os.environ.get("REDDIT_REPLACE_MORE_LIMIT", "32"))
        self.replace_more_limit = replace_more_limit

    def _submission(self, url_or_id: str):
        if re.match(r"^https?://", url_or_id):
            return self.reddit.submission(url=url_or_id)
        return self.reddit.submission(id=url_or_id.removeprefix("t3_"))

    def get_post(self, url_or_id: str) -> dict:
        s = self._submission(url_or_id)
        return {
            "id": s.id,
            "title": s.title,
            "subreddit": s.subreddit.display_name,
            "author": _author_name(s),
            "selftext": s.selftext,
            "url": f"https://www.reddit.com{s.permalink}",
            "created_utc": s.created_utc,
            "num_comments": s.num_comments,
            "score": s.score,
            "locked": s.locked,
        }

    def get_comments(self, post_id: str) -> list[dict]:
        s = self._submission(post_id)
        s.comment_sort = "new"
        s.comments.replace_more(limit=self.replace_more_limit)
        out = []
        for c in s.comments.list():
            out.append({
                "id": c.id,
                "parent_id": c.parent_id,  # t1_<comment> or t3_<post>
                "author": _author_name(c),
                "body": c.body,
                "created_utc": c.created_utc,
                "score": c.score,
                "permalink": f"https://www.reddit.com{c.permalink}",
                "is_submitter": c.is_submitter,
                "distinguished": c.distinguished,
                "edited": bool(c.edited),
                "removed": c.body in ("[removed]", "[deleted]"),
                "depth": getattr(c, "depth", None),
            })
        return out

    def get_author(self, name: str) -> dict:
        info = {"name": name}
        try:
            r = self.reddit.redditor(name)
            if getattr(r, "is_suspended", False):
                info["suspended"] = True
                return info
            info.update(created_utc=r.created_utc, link_karma=r.link_karma, comment_karma=r.comment_karma)
        except Exception:  # deleted/shadowbanned accounts 404
            info["suspended"] = True
        return info

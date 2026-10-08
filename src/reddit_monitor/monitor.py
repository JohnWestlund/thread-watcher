"""Core monitoring logic, independent of MCP so it can be tested and run from a CLI."""

from __future__ import annotations

import os

from . import signals
from .store import Store

EXCERPT = 400
MAX_AUTHOR_LOOKUPS_PER_CHECK = 25
RSS_LIMITATIONS = (
    "RSS mode: reply threading, scores, and author account age/karma are unavailable, and replies "
    "you post aren't detected. Open permalinks when context matters, and mark comments 'replied' "
    "once you've answered them."
)


def _excerpt(text: str, n: int = EXCERPT) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


class Monitor:
    def __init__(self, client, store: Store, default_owner: str | None = None):
        self.client = client
        self.store = store
        self.default_owner = default_owner if default_owner is not None else os.environ.get("REDDIT_USERNAME")

    def watch(self, url_or_id: str, owner: str | None = None, include_existing: bool = True) -> dict:
        post = self.client.get_post(url_or_id)
        owner = owner or self.default_owner or post["author"]
        self.store.watch(post["id"], post["url"], post["title"], post["subreddit"], owner)
        result = {"post": {k: post[k] for k in ("id", "title", "subreddit", "author", "url", "num_comments")},
                  "owner": owner}
        if post["author"].lower() != owner.lower():
            result["warning"] = (f"Post was written by u/{post['author']}, not u/{owner}. Comments are "
                                 f"triaged as if u/{owner} is the one replying.")
        if not include_existing:
            for c in self.client.get_comments(post["id"]):
                self.store.record_comment(c, post["id"])
                self.store.set_status(c["id"], "skipped", note="existed before watching")
            self.store.mark_checked(post["id"])
        return result

    def check(self, post_id: str | None = None) -> dict:
        posts = self.store.watched(post_id)
        if post_id and not posts:
            raise ValueError(f"Post {post_id} is not being watched. Call watch_post first.")
        lookups = 0
        report = []
        for p in posts:
            owner = p["owner"]
            comments = self.client.get_comments(p["post_id"])
            by_id = {c["id"]: c for c in comments}
            owner_replied_to = {c["parent_id"][3:] for c in comments
                                if c["author"].lower() == owner.lower() and c["parent_id"].startswith("t1_")}

            # Anything the owner has since answered on Reddit is done.
            for row in self.store.comments(p["post_id"]):
                if row["comment_id"] in owner_replied_to and row["status"] in ("new", "drafted", "needs_owner"):
                    self.store.set_status(row["comment_id"], "replied")

            new = []
            for c in comments:
                if self.store.is_seen(c["id"]) or c["author"].lower() == owner.lower():
                    continue
                self.store.record_comment(c, p["post_id"])
                if c["id"] in owner_replied_to:
                    self.store.set_status(c["id"], "replied", note="already answered when first seen")
                    continue

                author_info = None
                if c["author"] != "[deleted]" and getattr(self.client, "supports_author_info", True):
                    author_info = self.store.cached_author(c["author"])
                    if author_info is None and lookups < MAX_AUTHOR_LOOKUPS_PER_CHECK:
                        author_info = self.client.get_author(c["author"])
                        self.store.cache_author(author_info)
                        lookups += 1
                sig = signals.compute(c, by_id, owner, author_info)
                if sig["suggested_bucket"] == "skip":
                    self.store.set_status(c["id"], "skipped", note="; ".join(sig["reasons"]))
                parent = by_id.get(c["parent_id"][3:]) if c["parent_id"].startswith("t1_") else None
                if parent:
                    replying_to = {"author": parent["author"], "excerpt": _excerpt(parent["body"])}
                elif c["parent_id"].startswith("t3_"):
                    replying_to = {"author": owner, "excerpt": "(your post)"}
                else:
                    replying_to = {"author": None, "excerpt": "(unknown in RSS mode; open the permalink)"}
                new.append({
                    "comment_id": c["id"],
                    "author": c["author"],
                    "body": c["body"],
                    "permalink": c["permalink"],
                    "created_utc": c["created_utc"],
                    "replying_to": replying_to,
                    "signals": sig,
                })
            self.store.mark_checked(p["post_id"])
            order = {"ask_owner": 0, "respond": 1, "fyi": 2, "skip": 3}
            new.sort(key=lambda x: (order[x["signals"]["suggested_bucket"]], x["created_utc"] or 0))
            entry = {
                "post_id": p["post_id"], "title": p["title"], "url": p["url"], "owner": owner,
                "new_comment_count": len(new),
                "new_comments": new,
            }
            if not getattr(self.client, "supports_threading", True):
                entry["limitations"] = RSS_LIMITATIONS
            report.append(entry)
        return {"posts": report}

    def context(self, comment_id: str) -> dict:
        rows = self.store.comments(comment_id=comment_id)
        if not rows:
            raise ValueError(f"Comment {comment_id} hasn't been seen by check_new_comments yet.")
        post_id = rows[0]["post_id"]
        post = self.client.get_post(post_id)
        by_id = {c["id"]: c for c in self.client.get_comments(post_id)}
        chain, cid = [], comment_id
        while cid and cid in by_id:
            c = by_id[cid]
            chain.append({"comment_id": c["id"], "author": c["author"], "body": c["body"], "score": c["score"]})
            cid = c["parent_id"][3:] if c["parent_id"].startswith("t1_") else None
        chain.reverse()
        replies = [{"comment_id": c["id"], "author": c["author"], "body": _excerpt(c["body"])}
                   for c in by_id.values() if c["parent_id"] == f"t1_{comment_id}"]
        return {
            "post": {"title": post["title"], "author": post["author"], "subreddit": post["subreddit"],
                     "body": post["selftext"], "url": post["url"]},
            "thread": chain,  # oldest first, ending with the comment itself
            "existing_replies": replies,
            "status": rows[0]["status"], "note": rows[0]["note"], "drafts": rows[0]["drafts"],
        }

    def pending(self, post_id: str | None = None) -> list[dict]:
        out = []
        for status in ("needs_owner", "drafted", "new"):
            out += self.store.comments(post_id, status)
        return out

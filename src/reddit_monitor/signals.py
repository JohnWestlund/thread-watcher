"""Cheap, explainable triage signals for a comment.

These are hints, not verdicts. The server suggests a bucket with reasons; Claude
reads the comment itself and makes the call, and anything ambiguous goes to the owner.

Buckets:
  respond   - addressed to the owner and worth a reply (question or substantive point)
  ask_owner  - a judgment call: possible troll/bad faith, hostile, long back-and-forth
  fyi       - side conversation between other users; no reply expected
  skip      - bots, deleted/removed, the owner's own comments, one-word reactions
"""

from __future__ import annotations

import re
import time

BOT_NAMES = {"automoderator", "remindmebot", "sneakpeekbot", "repostsleuthbot", "savevideo"}

# Deliberately small: these are flags for a human look, not a filter.
HOSTILE_PATTERNS = [
    r"\bidiot\w*", r"\bmoron\w*", r"\bstupid\b", r"\bdumb\w*", r"\bshill\w*", r"\bgrift\w*",
    r"\bclown\w*", r"\bpathetic\b", r"\bloser\b", r"\bkys\b", r"\bshut up\b", r"\bcope\b",
    r"\bseethe\b", r"\bfuck you\b", r"\bscam\w*", r"\bliar\b", r"\blying\b", r"\btroll\w*",
    r"\bnobody asked\b", r"\bratio\b", r"\bbot\b",
]
QUESTION_START = re.compile(
    r"^\s*(how|what|why|when|where|which|who|is|are|does|do|did|can|could|would|should|will|have|has|any)\b",
    re.I,
)

NEW_ACCOUNT_DAYS = 30
LOW_KARMA = 50
DOWNVOTED_SCORE = -3
ARGUMENT_TURNS = 3
SHORT_CHARS = 20


def _parent_comment_id(parent_id: str) -> str | None:
    return parent_id[3:] if parent_id.startswith("t1_") else None


def owner_turns_in_chain(comment: dict, by_id: dict[str, dict], owner: str) -> int:
    """How many times the owner has already spoken in this comment's ancestry
    alongside this same author - a proxy for 'we've been going back and forth'."""
    turns, author = 0, comment["author"]
    pid = _parent_comment_id(comment["parent_id"])
    while pid and pid in by_id:
        parent = by_id[pid]
        if parent["author"].lower() == owner.lower():
            turns += 1
        elif parent["author"] != author:
            break
        pid = _parent_comment_id(parent["parent_id"])
    return turns


def compute(comment: dict, by_id: dict[str, dict], owner: str, author_info: dict | None,
            now: float | None = None) -> dict:
    now = now or time.time()
    body = comment["body"] or ""
    author = comment["author"]
    owner_l = owner.lower()

    mentions_owner = f"u/{owner_l}" in body.lower()
    threading_known = comment["parent_id"] != "unknown"
    if threading_known:
        parent = by_id.get(_parent_comment_id(comment["parent_id"]) or "")
        top_level = comment["parent_id"].startswith("t3_")
        reply_to_owner = bool(parent and parent["author"].lower() == owner_l)
        addressed = top_level or reply_to_owner or mentions_owner
    else:
        # RSS mode: the feed doesn't say what a comment replies to. On the owner's
        # own post, assume it may be aimed at them rather than silently dropping it.
        top_level = reply_to_owner = None
        addressed = True

    s: dict = {
        "threading_known": threading_known,
        "top_level": top_level,
        "reply_to_owner": reply_to_owner,
        "mentions_owner": mentions_owner,
        "addressed_to_owner": addressed,
        "is_question": "?" in body or bool(QUESTION_START.search(body)),
        "length": len(body),
        "score": comment.get("score"),
        "hostile_terms": sorted({m.group(0).lower() for p in HOSTILE_PATTERNS for m in re.finditer(p, body, re.I)}),
        "caps_ratio": round(sum(ch.isupper() for ch in body) / max(1, sum(ch.isalpha() for ch in body)), 2),
        "is_bot": author.lower() in BOT_NAMES or author.lower().endswith("bot")
                  or comment.get("distinguished") == "moderator",
        # A deleted account can leave its comment text behind; only skip when the text is gone too.
        "removed": comment.get("removed", False),
        "owner_turns_in_chain": owner_turns_in_chain(comment, by_id, owner) if reply_to_owner else 0,
        "author_comments_in_thread": sum(1 for c in by_id.values() if c["author"] == author),
    }
    if author_info:
        s["author_suspended"] = bool(author_info.get("suspended"))
        if author_info.get("created_utc"):
            s["account_age_days"] = int((now - author_info["created_utc"]) / 86400)
        if author_info.get("link_karma") is not None:
            s["author_karma"] = (author_info.get("link_karma") or 0) + (author_info.get("comment_karma") or 0)

    s["suggested_bucket"], s["reasons"] = _bucket(s, author.lower() == owner_l)
    return s


def _bucket(s: dict, is_owner: bool) -> tuple[str, list[str]]:
    if is_owner:
        return "skip", ["your own comment"]
    if s["removed"]:
        return "skip", ["deleted or removed"]
    if s["is_bot"]:
        return "skip", ["bot or moderator notice"]

    flags = []
    if s["hostile_terms"]:
        flags.append(f"hostile language: {', '.join(s['hostile_terms'])}")
    if s.get("author_suspended"):
        flags.append("author account suspended or deleted")
    young = s.get("account_age_days") is not None and s["account_age_days"] < NEW_ACCOUNT_DAYS
    low_karma = s.get("author_karma") is not None and s["author_karma"] < LOW_KARMA
    if young and low_karma:
        flags.append(f"new low-karma account ({s['account_age_days']}d, {s['author_karma']} karma)")
    if s["score"] is not None and s["score"] <= DOWNVOTED_SCORE:
        flags.append(f"heavily downvoted ({s['score']})")
    if s["owner_turns_in_chain"] >= ARGUMENT_TURNS:
        flags.append(f"you've already replied {s['owner_turns_in_chain']} times in this exchange")
    if s["caps_ratio"] > 0.6 and s["length"] > 20:
        flags.append("mostly caps")

    if not s["addressed_to_owner"]:
        return ("ask_owner" if flags else "fyi"), flags or ["side conversation between other users"]
    if flags:
        return "ask_owner", flags
    if s["is_question"]:
        return "respond", ["question directed at you"]
    if s["length"] >= 80:
        return "respond", ["substantive comment directed at you"]
    if s["length"] < SHORT_CHARS:
        return "skip", ["brief reaction; a reply is optional"]
    return "respond", ["short comment directed at you; a quick reply may fit"]

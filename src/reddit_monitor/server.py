"""MCP server exposing the Reddit thread monitor to Claude.

Read-only by design: there is no tool that posts, votes, or edits. Drafts are
stored locally and handed to the owner, who posts them himself.
"""

from __future__ import annotations

import os
from pathlib import Path

import functools

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp.exceptions import ToolError

from .monitor import Monitor
from .store import Store

INSTRUCTIONS = """\
Monitors Reddit posts the user (the owner) wrote and surfaces new comments from other users.
This server is read-only. Never post to Reddit; replies are drafts the user posts themselves.

Workflow (or use the `triage_and_draft` prompt):
1. check_new_comments to get unseen comments with triage signals and a suggested_bucket.
2. Treat suggested_bucket as a hint. Read each comment yourself (get_comment_context for the
   full exchange) and decide:
   - respond: draft 1-3 distinct reply options in the owner's voice (use a writing-voice skill such as my-voice when
     available), then save_drafts.
   - ask_owner: any doubt about whether engaging is worthwhile (possible troll, bad faith,
     hostile, heated back-and-forth, touchy topic). Do not draft; set_comment_status
     'needs_owner' with a one-line note on why, and bring it to the owner.
   - fyi / skip: set_comment_status 'skipped' with a short note.
3. Present results grouped: needs your call, drafted replies (with permalinks), and a count of
   skipped/side-conversation comments.
"""

_server = _Server("reddit-monitor", instructions=INSTRUCTIONS)


class _ToolRegistry:
    """Registers tools so any exception reaches Claude as a readable message.
    (MCP SDK v2 otherwise reports unexpected errors as just "Error executing tool".)"""

    def tool(self):
        def register(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                try:
                    return fn(*args, **kwargs)
                except ToolError:
                    raise
                except Exception as e:
                    raise ToolError(f"{type(e).__name__}: {e}") from e

            return _server.tool()(wrapper)

        return register

    def __getattr__(self, name):
        return getattr(_server, name)


server = _ToolRegistry()
_monitor: Monitor | None = None


def backend() -> str:
    """'api' (PRAW, needs an approved Reddit app) or 'rss' (public comment feed, no credentials).
    REDDIT_BACKEND wins; otherwise use the API when credentials are present."""
    choice = (os.environ.get("REDDIT_BACKEND") or "").lower()
    if choice in ("api", "rss"):
        return choice
    return "api" if os.environ.get("REDDIT_CLIENT_ID") else "rss"


class _LazyClient:
    """Connects to Reddit on first use, so local-only tools work without credentials."""

    def __init__(self):
        self._client = None

    def _get(self):
        if self._client is None:
            if backend() == "api":
                from .reddit_client import RedditClient

                self._client = RedditClient()
            else:
                from .rss_client import RSSClient

                self._client = RSSClient()
        return self._client

    def __getattr__(self, name):
        if name in ("supports_author_info", "supports_threading"):
            return getattr(self._get(), name, True)
        return getattr(self._get(), name)


def monitor() -> Monitor:
    global _monitor
    if _monitor is None:
        db = os.environ.get("REDDIT_MONITOR_DB") or str(Path.home() / ".reddit-monitor" / "state.db")
        _monitor = Monitor(_LazyClient(), Store(db))
    return _monitor


@server.tool()
def watch_post(url: str, owner: str | None = None, include_existing: bool = True) -> dict:
    """Start watching a Reddit post for new comments.

    url: post URL or id. owner: Reddit username treated as "you" (defaults to REDDIT_USERNAME,
    then the post's author). include_existing: if false, comments already on the post are
    marked as seen so only future comments are surfaced.
    """
    return monitor().watch(url, owner, include_existing)


@server.tool()
def unwatch_post(post_id: str) -> dict:
    """Stop watching a post. Its comment history stays in the local database."""
    return {"removed": monitor().store.unwatch(post_id)}


@server.tool()
def list_watched_posts() -> list[dict]:
    """List watched posts with their owner and last check time."""
    return monitor().store.watched()


@server.tool()
def check_new_comments(post_id: str | None = None) -> dict:
    """Fetch comments posted since the last check, by anyone other than the owner.

    Each comment includes what it replies to and triage signals: whether it's addressed to the
    owner, whether it's a question, hostile terms, author account age/karma, score, how many
    times the owner has already replied in that exchange, and a suggested_bucket
    (ask_owner / respond / fyi / skip) with reasons. Bots, deleted comments, and one-word
    reactions are auto-marked skipped. Comments the owner has since replied to on Reddit are
    marked replied. Checks all watched posts when post_id is omitted.
    """
    return monitor().check(post_id)


@server.tool()
def get_comment_context(comment_id: str) -> dict:
    """Full context for drafting: the post, the comment chain from top level down to this
    comment, any replies under it, and its current status and saved drafts."""
    return monitor().context(comment_id)


@server.tool()
def save_drafts(comment_id: str, drafts: list[str], note: str | None = None) -> dict:
    """Store 1-3 draft replies for a comment and mark it 'drafted'. Nothing is posted."""
    if not 1 <= len(drafts) <= 3:
        raise ValueError("Provide between 1 and 3 drafts.")
    return {"saved": monitor().store.set_status(comment_id, "drafted", note=note, drafts=drafts)}


@server.tool()
def set_comment_status(comment_id: str, status: str, note: str | None = None) -> dict:
    """Set a comment's status: new, drafted, needs_owner, skipped, or replied. Use 'needs_owner' for judgment
    calls (with a note on why), 'skipped' for no-reply, 'replied' once the owner has posted."""
    return {"updated": monitor().store.set_status(comment_id, status, note=note)}


@server.tool()
def list_pending(post_id: str | None = None) -> list[dict]:
    """Comments still waiting on the owner: needs_owner first, then drafted, then new."""
    return monitor().pending(post_id)


@server.prompt()
def triage_and_draft(post_url: str = "") -> str:
    """Check watched posts, triage new comments, and draft replies in the owner's voice."""
    target = f"Watch {post_url} first if it isn't already watched, then check it." if post_url \
        else "Check all watched posts."
    return f"""{target}

For every new comment from check_new_comments:
- If there's any question about whether engaging is worthwhile (troll, bad faith, hostile,
  repeated back-and-forth, sensitive topic), do NOT draft. Mark it needs_owner with a one-line
  reason and list it for me.
- If it deserves a reply, pull get_comment_context, then write 1-3 genuinely different reply
  options in my voice, using a writing-voice skill like my-voice if available (e.g. short and direct, fuller explanation, light/friendly).
  Keep each one Reddit-appropriate and grounded in what I actually said in the post. Save them
  with save_drafts.
- Otherwise mark it skipped with a short note.

Never post anything to Reddit. Finish with: comments needing my call (with links and why),
drafted replies (with links), and a one-line count of what was skipped."""


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()

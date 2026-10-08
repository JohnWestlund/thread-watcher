# Reddit thread monitor (MCP server)

Watches Reddit posts you wrote, picks out new comments from other people, and gives Claude what it needs to triage them and draft replies in your voice. It is read-only: there is no tool that posts, votes, or edits. You post every reply yourself.

## How it works

1. `watch_post` adds a post. "You" defaults to `REDDIT_USERNAME`, then to the post's author.
2. `check_new_comments` returns comments since the last check (not yours), each with what it replies to and triage signals:
   - addressed to you (top-level, reply to your comment, or mentions u/you), question or not, length
   - hostile terms, caps ratio, score, author account age and karma
   - how many times you've already replied in that exchange
   - a `suggested_bucket` with reasons: `ask_owner`, `respond`, `fyi` (others talking to each other), `skip` (bots, deleted, "nice")
3. Claude reads each comment itself. Anything where engaging is a judgment call is marked `needs_owner` with a reason and **no draft**. Comments worth answering get 1 to 3 reply options written in your voice (using a writing-style skill such as my-voice if you have one) and stored with `save_drafts`.
4. When you reply on Reddit, the next check sees it and marks that comment `replied`.

The `triage_and_draft` prompt runs that whole loop. State lives in SQLite at `~/.reddit-monitor/state.db` (override with `REDDIT_MONITOR_DB`).

## Tools

| Tool | What it does |
|---|---|
| `watch_post(url, owner?, include_existing=true)` | Start watching. `include_existing=false` ignores comments already there. |
| `check_new_comments(post_id?)` | New comments with signals, judgment calls sorted first. |
| `get_comment_context(comment_id)` | Post body, the full chain down to the comment, replies under it, saved drafts. |
| `save_drafts(comment_id, drafts)` | Store 1 to 3 drafts; status becomes `drafted`. |
| `set_comment_status(comment_id, status, note?)` | `new`, `drafted`, `needs_owner`, `skipped`, `replied`. |
| `list_pending(post_id?)` | What's still waiting on you. |
| `list_watched_posts`, `unwatch_post` | Manage the watch list. |

## Two ways to read Reddit

- **API mode** (full features): uses an approved Reddit app through PRAW. Chosen automatically when `REDDIT_CLIENT_ID` is set.
- **RSS mode** (no credentials): reads the post's public comment feed (the post URL + `.rss`). Use it while waiting for API approval. The feed is flat, so it can't tell what a comment is replying to, has no scores or author age/karma, and doesn't notice when you've replied. Every comment on your post is treated as possibly aimed at you, and you tell Claude "I replied to that one" to close it out. The feed holds the newest 100 comments, so on a busy thread check often enough to not miss any.

Set `REDDIT_BACKEND=rss` or `=api` to force one.

## Setup

1. Create a Reddit app at <https://www.reddit.com/prefs/apps>, type **script**. Note the client id (under the app name) and the secret. Reddit may ask you to register or get approval for API access first.
2. Install: `pip install -e .` (Python 3.10+). For RSS mode, skip step 1 and leave out the id and secret below; just set `REDDIT_USERNAME`.
3. Add it to Claude Code:

   ```bash
   claude mcp add reddit-monitor \
     -e REDDIT_CLIENT_ID=... -e REDDIT_CLIENT_SECRET=... -e REDDIT_USERNAME=your_username \
     -- reddit-monitor-mcp
   ```

   Or in Claude Desktop's `claude_desktop_config.json`:

   ```json
   {
     "mcpServers": {
       "reddit-monitor": {
         "command": "reddit-monitor-mcp",
         "env": {
           "REDDIT_CLIENT_ID": "...",
           "REDDIT_CLIENT_SECRET": "...",
           "REDDIT_USERNAME": "your_username"
         }
       }
     }
   }
   ```

The server only uses app-only OAuth (id + secret, no password), so the credentials can't post even if something asked them to.

## Using it with Claude

Once the server is added, talk to Claude normally:

- "Watch https://www.reddit.com/r/.../comments/abc123/..." (add "ignore the comments already there" to only see future ones)
- "Check my Reddit post and draft replies." This runs the `triage_and_draft` prompt. In Claude Code you can also type `/mcp__reddit-monitor__triage_and_draft`.
- "What's still waiting on me?" lists judgment calls and saved drafts.
- "Show me the whole exchange for that comment" or "give me a shorter option for the second one" to iterate on a draft.
- "I replied to that one" or "skip it" updates the status. Replies you post are also picked up on the next check.

To debug, it helps to have a second Reddit account (or a friend) leave a few deliberate test comments: a real question, a one-word "nice", a mildly hostile one, and a reply chain of three or more back-and-forths. Each should land in a different bucket.

## Running it on a schedule

Ask Claude to run the `triage_and_draft` prompt on an interval (for example every 30 minutes while a post is fresh). Each run only looks at comments it hasn't seen.

## Tuning

Thresholds are at the top of `src/reddit_monitor/signals.py`: new-account age (30 days), low karma (50), heavily downvoted (-3), back-and-forth turns before it's flagged (3), and the hostile-term list. They only change the suggestion; Claude still reads every comment.

`REDDIT_REPLACE_MORE_LIMIT` (default 32) caps how many "load more comments" expansions a check does on big threads.

## Tests

```bash
pip install -e '.[dev]' && pytest
```

Tests use a fake Reddit client and hand-built feeds. Neither the live PRAW path nor the live RSS feed has been run against Reddit yet.

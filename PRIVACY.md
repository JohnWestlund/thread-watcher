# Privacy and data handling

thread-watcher is a personal, non-commercial tool. Each person runs their own copy on their own computer. There is no hosted service and nothing is sent to the author of this project.

## What it reads

Only public Reddit data: the posts it is told to watch, their comments, and, in API mode, a commenter's public account age and karma. It reads through Reddit's API with the owner's approved app credentials, or through Reddit's public RSS feeds. It never posts, comments, votes, messages, or edits. In API mode it uses app-only OAuth, which can't post.

## Where the data goes

- **Your computer.** Comment IDs, text, author names, triage status, and any saved draft replies are stored in a local SQLite file (`~/.reddit-monitor/state.db`). Unwatching a post stops new reads; delete that file to remove everything.
- **Your AI assistant.** The tool is an MCP server. The assistant you connect it to (for example Claude, under your own account) receives the comment text so it can sort comments and suggest replies for you to review. That content is handled under your assistant provider's terms, so check your account's settings for whether conversations may be used for model training.

Nothing is sold, shared, published, or used by this project to train any model.

## What it doesn't do

- Infer sensitive characteristics such as health, politics, or religion.
- Link Reddit accounts to identities outside Reddit.
- Exceed or work around Reddit's rate limits. It paces requests and backs off when Reddit says to.

See Reddit's [Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy).

from reddit_monitor.monitor import Monitor
from reddit_monitor.rss_client import RSSClient, _post_url, parse_feed
from reddit_monitor.store import Store


def entry(id, author, html_body, title="re: I built a thing", link=None, published="2026-10-08T10:00:00+00:00"):
    link = link or f"https://www.reddit.com/r/Python/comments/abc123/i_built_a_thing/{id[3:]}/"
    body = html_body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<entry>
  <author><name>/u/{author}</name><uri>https://www.reddit.com/user/{author}</uri></author>
  <category term="Python" label="r/Python"/>
  <content type="html">{body}</content>
  <id>{id}</id>
  <link href="{link}"/>
  <updated>{published}</updated>
  <published>{published}</published>
  <title>{title}</title>
</entry>"""


def feed(*entries):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">
<category term="Python" label="r/Python"/>
<id>/r/Python/comments/abc123/i_built_a_thing/.rss?sort=new</id>
<title>I built a thing : Python</title>
{''.join(entries)}
</feed>"""


POST = entry("t3_abc123", "Xenther",
             '<!-- SC_OFF --><div class="md"><p>Here is how it works &amp; why.</p></div><!-- SC_ON --> '
             '&#32; submitted by &#32; <a href="https://www.reddit.com/user/Xenther"> /u/Xenther </a>',
             title="I built a thing", link="https://www.reddit.com/r/Python/comments/abc123/i_built_a_thing/")


def test_parse_feed_extracts_post_and_comments():
    xml = feed(POST,
               entry("t1_c1", "alice", '<div class="md"><p>How does it handle <code>rate limits</code>?</p></div>'),
               entry("t1_c2", "bob", '<div class="md"><blockquote><p>why</p></blockquote><p>Because.</p></div>'))
    post, comments = parse_feed(xml)
    assert post["id"] == "abc123" and post["author"] == "Xenther" and post["subreddit"] == "Python"
    assert post["selftext"] == "Here is how it works & why."
    assert [c["id"] for c in comments] == ["c1", "c2"]
    assert comments[0]["body"] == "How does it handle rate limits?"
    assert comments[0]["parent_id"] == "unknown"
    assert "> why" in comments[1]["body"]
    assert comments[0]["created_utc"] == 1791453600


def test_post_url_normalization():
    assert _post_url("https://old.reddit.com/r/Python/comments/abc123/x/?utm=1") == \
        "https://www.reddit.com/r/Python/comments/abc123/x/"
    assert _post_url("abc123") == "https://www.reddit.com/comments/abc123/"


def test_monitor_in_rss_mode():
    pages = [feed(POST, entry("t1_c1", "alice", "<p>How does it handle rate limits?</p>"),
                  entry("t1_c2", "troll", "<p>this is stupid, nobody asked</p>"),
                  entry("t1_c3", "Xenther", "<p>Backoff with jitter.</p>"))]
    requested = []

    def fetch(url):
        requested.append(url)
        return pages[-1]

    client = RSSClient(fetch=fetch, min_interval=0)
    m = Monitor(client, Store(":memory:"), default_owner="")
    w = m.watch("https://www.reddit.com/r/Python/comments/abc123/i_built_a_thing/")
    assert w["owner"] == "Xenther"
    report = m.check()["posts"][0]
    assert "limitations" in report
    b = {x["comment_id"]: x["signals"]["suggested_bucket"] for x in report["new_comments"]}
    assert b == {"c1": "respond", "c2": "ask_owner"}  # owner's own comment never surfaces
    assert requested[0].endswith(".rss?sort=new&limit=100")

    pages.append(feed(POST, entry("t1_c4", "dana", "<p>Does it work on Windows?</p>")))
    assert [x["comment_id"] for x in m.check()["posts"][0]["new_comments"]] == ["c4"]
    assert m.context("c4")["thread"][0]["body"] == "Does it work on Windows?"

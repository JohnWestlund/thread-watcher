import time

import pytest

from reddit_monitor.monitor import Monitor
from reddit_monitor.store import Store

NOW = time.time()
OLD = NOW - 3 * 365 * 86400


def c(id, author, body, parent="t3_post1", score=1, **kw):
    return {"id": id, "parent_id": parent, "author": author, "body": body, "created_utc": NOW - 100,
            "score": score, "permalink": f"https://www.reddit.com/r/test/comments/post1/_/{id}/",
            "is_submitter": author == "john", "distinguished": None, "edited": False,
            "removed": body in ("[removed]", "[deleted]"), "depth": 0, **kw}


class FakeClient:
    def __init__(self, comments, authors=None):
        self.comments = comments
        self.authors = authors or {}
        self.author_calls = 0

    def get_post(self, url_or_id):
        return {"id": "post1", "title": "I built a thing", "subreddit": "test", "author": "john",
                "selftext": "Here's how it works...", "url": "https://www.reddit.com/r/test/comments/post1/",
                "created_utc": OLD, "num_comments": len(self.comments), "score": 10, "locked": False}

    def get_comments(self, post_id):
        return list(self.comments)

    def get_author(self, name):
        self.author_calls += 1
        return self.authors.get(name, {"name": name, "created_utc": OLD, "link_karma": 500, "comment_karma": 2000})


@pytest.fixture
def setup():
    def make(comments, authors=None):
        client = FakeClient(comments, authors)
        m = Monitor(client, Store(":memory:"), default_owner="john")
        m.watch("https://www.reddit.com/r/test/comments/post1/")
        return m, client
    return make


def buckets(report):
    return {x["comment_id"]: x["signals"]["suggested_bucket"] for x in report["posts"][0]["new_comments"]}


def test_triage_buckets(setup):
    m, _ = setup([
        c("q1", "alice", "How did you handle rate limiting? I tried something similar."),
        c("t1", "troll99", "This is stupid, you're clearly a shill."),
        c("s1", "bob", "nice"),
        c("bot", "AutoModerator", "Your post has been approved."),
        c("j1", "john", "Thanks!", parent="t1_q1"),
        c("q2", "dana", "Would this work for subreddit-wide monitoring too, or just single posts?"),
        c("side", "carol", "I agree with alice on this, it's a solid approach overall.", parent="t1_x9"),
    ])
    b = buckets(m.check())
    # q1 is absent because john already answered it
    assert b == {"q2": "respond", "t1": "ask_owner", "s1": "skip", "bot": "skip", "side": "fyi"}


def test_owner_already_replied_marks_replied(setup):
    m, _ = setup([
        c("q1", "alice", "How did you handle rate limiting?"),
        c("j1", "john", "Backoff with jitter.", parent="t1_q1"),
    ])
    report = m.check()
    assert report["posts"][0]["new_comment_count"] == 0
    assert m.store.comments(comment_id="q1")[0]["status"] == "replied"


def test_only_new_comments_surface_on_second_check(setup):
    comments = [c("q1", "alice", "What library did you use for this?")]
    m, client = setup(comments)
    assert len(m.check()["posts"][0]["new_comments"]) == 1
    client.comments.append(c("q2", "dave", "Does it work on Windows too?"))
    new = m.check()["posts"][0]["new_comments"]
    assert [x["comment_id"] for x in new] == ["q2"]


def test_owner_reply_after_draft_closes_it(setup):
    m, client = setup([c("q1", "alice", "What library did you use?")])
    m.check()
    m.store.set_status("q1", "drafted", drafts=["PRAW, read-only."])
    client.comments.append(c("j1", "john", "PRAW!", parent="t1_q1"))
    m.check()
    assert m.store.comments(comment_id="q1")[0]["status"] == "replied"
    assert m.pending() == []


def test_new_low_karma_account_needs_owner(setup):
    m, _ = setup(
        [c("q1", "fresh", "Why would anyone use this instead of the official tool?")],
        authors={"fresh": {"name": "fresh", "created_utc": NOW - 2 * 86400, "link_karma": 1, "comment_karma": 3}},
    )
    item = m.check()["posts"][0]["new_comments"][0]
    assert item["signals"]["suggested_bucket"] == "ask_owner"
    assert any("new low-karma" in r for r in item["signals"]["reasons"])


def test_long_back_and_forth_needs_owner(setup):
    m, _ = setup([
        c("a1", "eve", "Why not just use the API directly?"),
        c("j1", "john", "PRAW handles auth and pagination.", parent="t1_a1"),
        c("a2", "eve", "That's not a real reason though, is it?", parent="t1_j1"),
        c("j2", "john", "It saves a lot of boilerplate.", parent="t1_a2"),
        c("a3", "eve", "Boilerplate is fine, why add a dependency?", parent="t1_j2"),
        c("j3", "john", "Fair, it's a tradeoff.", parent="t1_a3"),
        c("a4", "eve", "So you admit it was pointless?", parent="t1_j3"),
    ])
    item = next(x for x in m.check()["posts"][0]["new_comments"] if x["comment_id"] == "a4")
    assert item["signals"]["owner_turns_in_chain"] == 3
    assert item["signals"]["suggested_bucket"] == "ask_owner"


def test_include_existing_false_only_surfaces_future(setup):
    client = FakeClient([c("old", "alice", "What library did you use?")])
    m = Monitor(client, Store(":memory:"), default_owner="john")
    m.watch("post1", include_existing=False)
    assert m.check()["posts"][0]["new_comments"] == []


def test_context_and_drafts(setup):
    m, _ = setup([
        c("a1", "alice", "Cool project. What's next?"),
        c("a2", "bob", "Also, will you open source it?", parent="t1_a1"),
    ])
    m.check()
    m.store.set_status("a2", "drafted", drafts=["Yes, soon.", "Planning to once it's stable."])
    ctx = m.context("a2")
    assert [x["comment_id"] for x in ctx["thread"]] == ["a1", "a2"]
    assert ctx["drafts"] == ["Yes, soon.", "Planning to once it's stable."]
    assert ctx["post"]["title"] == "I built a thing"


def test_author_lookups_are_cached(setup):
    m, client = setup([c("q1", "alice", "How?"), c("q2", "alice", "And why?")])
    m.check()
    assert client.author_calls == 1


def test_warns_when_owner_is_not_author():
    m = Monitor(FakeClient([]), Store(":memory:"), default_owner="someone_else")
    assert "warning" in m.watch("post1")

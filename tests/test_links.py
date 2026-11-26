"""The link parser: each known form of a post or a comment, and a clear error for every other one."""
import pytest

from linkedin_publisher import links
from linkedin_publisher.api import PublisherError
from linkedin_publisher.links import LinkError, Target

P, C = "7400000000000000001", "7400000000000000002"
ACTIVITY = f"urn:li:activity:{P}"
ON_ACTIVITY = Target("post", ACTIVITY, ACTIVITY)
COMMENT = Target("comment", f"urn:li:comment:({ACTIVITY},{C})", ACTIVITY)
FEED = f"https://www.linkedin.com/feed/update/{ACTIVITY}/"
DASH = f"dashCommentUrn=urn%3Ali%3Afsd_comment%3A%28{C}%2Curn%3Ali%3Aactivity%3A{P}%29"
SHORT = f"commentUrn=urn%3Ali%3Acomment%3A%28activity%3A{P}%2C{C}%29"
FULL = f"commentUrn=urn%3Ali%3Acomment%3A%28urn%3Ali%3Aactivity%3A{P}%2C{C}%29"


@pytest.mark.parametrize("value, target", [
    (FEED, ON_ACTIVITY),
    (f"https://www.linkedin.com/feed/update/urn:li:ugcPost:{P}", Target("post", f"urn:li:ugcPost:{P}", None)),
    (f"https://linkedin.com/feed/update/urn%3Ali%3Ashare%3A{P}/?utm_source=x", Target("post", f"urn:li:share:{P}", None)),
    (f"https://www.linkedin.com/posts/someone_how-to-share-files-share-{P}-AbCd/", Target("post", f"urn:li:share:{P}", None)),
    (f"https://nl.linkedin.com/posts/someone_slug-activity-{P}-x_Y-?utm_medium=member", ON_ACTIVITY),
    (f"https://www.linkedin.com/posts/someone_my-share-2024-recap-activity-{P}/", ON_ACTIVITY),   # share-2024 is no id
    (f"{FEED}?{DASH}", COMMENT),
    (f"{FEED}?{SHORT}", COMMENT),
    (f"{FEED}?{FULL}&{DASH}&utm_source=share", COMMENT),
    (f"https://www.linkedin.com/feed/update/urn:li:ugcPost:1/?{DASH}", COMMENT),
    (ACTIVITY, ON_ACTIVITY),
    (f"  urn:li:ugcPost:{P}\n", Target("post", f"urn:li:ugcPost:{P}", None)),
    (f"urn:li:comment:(activity:{P},{C})", COMMENT),
    (f"urn:li:comment:({ACTIVITY},{C})", COMMENT),
    (f"urn:li:fsd_comment:({C},{ACTIVITY})", COMMENT),
    (f"urn%3Ali%3Acomment%3A%28activity%3A{P}%2C{C}%29", COMMENT),
])
def test_known_forms(value, target):
    assert links.parse_target(value) == target


@pytest.mark.parametrize("value, says", [
    ("", "not a LinkedIn link"),
    ("https://www.linkedin.com/in/someone/", "no post in this link"),
    (f"https://www.linkedin.com/posts/someone_slug-{P}/", "no post in this link"),
    ("https://www.linkedin.com/posts/someone_slug-share-12345-AbCd/", "no post in this link"),
    (f"https://www.linkedin.com/posts/someone_slug-activity-{P}1-AbCd/", "no post in this link"),        # 20 digits
    (f"https://www.linkedin.com/posts/someone_x-activity-{P}-and-many-more-words/", "no post in this link"),
    (f"https://www.linkedin.com/posts/someone_x-share-{C}-activity-{P}-AbCd/", "more than one post"),
    (f"{FEED}?commentUrn=", "empty comment parameter"),
    (f"{FEED}?commentUrn=%20&{DASH}", "empty comment parameter"),
    (f"{FEED}?{SHORT}&replyUrn=", "one level of answers"),
    (f"{FEED}?commentId={C}", "unknown comment parameter"),
    (f"{FEED}?{SHORT}&CommentUrn=x", "unknown comment parameter"),
    (f"{FEED}#{SHORT}", "fragment"),
    (f"{FEED}?{SHORT}#x", "fragment"),
    (f"https://www.linkedin.com/feed/update/urn:li:person:{P}/", "no post in this link"),
    (f"https://linkedin.com.example.org/feed/update/{ACTIVITY}/", "not a LinkedIn link"),
    (f"https://example.org/www.linkedin.com/feed/update/{ACTIVITY}/", "not a LinkedIn link"),
    (f"ftp://www.linkedin.com/feed/update/{ACTIVITY}/", "not a LinkedIn link"),
    ("https://lnkd.in/abcDEF", "short link"),
    (f"urn:li:comment:(ugcPost:{P},{C})", "on an activity"),
    (f"urn:li:comment:(urn:li:share:{P},{C})", "on an activity"),
    (f"urn:li:person:{P}", "unknown URN"),
    ("urn:li:activity:١٢", "unknown URN"),          # digits, but not ASCII ones
    (f"{FEED}?{SHORT}&replyUrn=urn%3Ali%3Acomment%3A%28activity%3A{P}%2C9%29", "one level of answers"),
    (f"https://www.linkedin.com/feed/update/urn:li:activity:9/?{DASH}", "belongs to"),
    (f"{FEED}?commentUrn=urn%3Ali%3Acomment%3A%28activity%3A{P}%2C9%29&{DASH}", "two different comments"),
    (f"{FEED}?commentUrn={ACTIVITY}", "not a comment"),
])
def test_every_other_form_is_refused(value, says):
    with pytest.raises(LinkError, match=says) as caught:
        links.parse_target(value)
    assert isinstance(caught.value, PublisherError)


def test_a_short_link_is_resolved_with_the_fetcher_it_is_given():
    hops = {"https://lnkd.in/abc": "https://lnkd.in/def", "https://lnkd.in/def": FEED}
    asked = []
    assert links.resolve_short_link(" https://lnkd.in/abc ", lambda url: asked.append(url) or hops.get(url)) == FEED
    assert asked == ["https://lnkd.in/abc", "https://lnkd.in/def"]
    assert links.resolve_short_link(FEED, lambda url: pytest.fail("a full link needs no call")) == FEED
    asked.clear()
    assert links.resolve_short_link("http://lnkd.in/abc", lambda url: asked.append(url) or hops.get(url)) == FEED
    assert asked == ["https://lnkd.in/abc", "https://lnkd.in/def"]           # never asked over plain http


def test_a_short_link_that_leads_nowhere_is_refused():
    with pytest.raises(LinkError, match="did not redirect"):
        links.resolve_short_link("https://lnkd.in/x", lambda url: None)
    with pytest.raises(LinkError, match="still redirects after 5 steps"):
        links.resolve_short_link("https://lnkd.in/x", lambda url: url)


def test_follow_redirect_reads_the_location_and_does_not_follow_it(site):
    target = site.serve(f"/feed/update/{ACTIVITY}/", "the post")
    assert links.follow_redirect(site.redirect("/abc", f"/feed/update/{ACTIVITY}/")) == target
    assert links.follow_redirect(site.serve("/page", "no redirect")) is None
    assert site.hits == ["/abc", "/page"]

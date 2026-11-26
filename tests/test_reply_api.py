"""The answer to a comment and the list that is read back. No request goes out: the pure parts are tested."""
import json
from types import SimpleNamespace

import pytest
import requests

from linkedin_publisher import api

ACTIVITY = "urn:li:activity:1"
COMMENT = "urn:li:comment:(urn:li:activity:1,2)"


def page(status=200, body=None):
    return SimpleNamespace(status_code=status, text=json.dumps(body), json=lambda: body)


def not_json(status=200):
    return SimpleNamespace(status_code=status, text="<html>", json=lambda: json.loads("<html>"))


def test_reply_payload_and_url():
    assert api.build_reply_payload("urn:li:person:x", ACTIVITY, COMMENT, "a [b] @c *d*") == {
        "actor": "urn:li:person:x", "object": ACTIVITY, "parentComment": COMMENT, "message": {"text": "a [b] @c *d*"}}
    assert api.comments_url(COMMENT) == ("https://api.linkedin.com/rest/socialActions/"
                                         "urn%3Ali%3Acomment%3A%28urn%3Ali%3Aactivity%3A1%2C2%29/comments")


def test_every_page_is_read_until_an_empty_one():
    pages = {0: page(body={"elements": [{"id": "1"}, {"id": "2"}]}), 2: page(body={"elements": [{"id": "3"}]}),
             3: page(body={"elements": [], "paging": {"start": 3}})}
    asked = []
    assert api.collect_comments(lambda start: asked.append(start) or pages[start]) == [{"id": "1"}, {"id": "2"}, {"id": "3"}]
    assert asked == [0, 2, 3]


def test_the_total_ends_the_list_without_another_request():
    asked = []
    body = {"elements": [{"id": "1"}, {"id": "2"}], "paging": {"start": 0, "count": 10, "total": 2}}
    assert len(api.collect_comments(lambda start: asked.append(start) or page(body=body))) == 2
    assert asked == [0]


def connection_lost(_start):
    raise requests.ConnectionError("reset")


@pytest.mark.parametrize("get_page, uncertain", [
    (lambda _s: page(403, {"message": "no"}), False), (lambda _s: page(429), False), (lambda _s: page(426), False),
    (lambda _s: page(500), True), (lambda _s: page(201, {"elements": []}), True),
    (lambda _s: page(body={"no": "list"}), True), (lambda _s: page(body=["x"]), True), (lambda _s: not_json(), True),
    (connection_lost, True),
])
def test_a_list_that_cannot_be_read_is_never_an_empty_one(get_page, uncertain):
    with pytest.raises(api.PublisherError) as caught:
        api.collect_comments(get_page)
    assert isinstance(caught.value, api.PublishUncertain) is uncertain


def test_a_list_that_does_not_end_is_unclear():
    with pytest.raises(api.PublishUncertain, match="did not end"):
        api.collect_comments(lambda start: page(body={"elements": [{"id": str(start)}]}))

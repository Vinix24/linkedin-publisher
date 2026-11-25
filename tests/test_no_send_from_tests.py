"""A test run never reaches LinkedIn: requests only gets to this machine, and the send paths refuse."""
import pytest
import requests

from linkedin_publisher import api


def test_a_request_beyond_this_machine_fails_and_loopback_still_works(site):
    session = requests.Session()
    session.trust_env = False      # no proxy lookup, so not even a DNS query leaves this machine
    # 192.0.2.1 is a documentation address that is never routed: without the block this times out
    # here, before the LinkedIn address is tried.
    for url in ("http://192.0.2.1/", "https://api.linkedin.com/rest/posts"):
        with pytest.raises(AssertionError, match="only this machine"):
            session.post(url, json={"commentary": "Dit is de posttekst."}, timeout=1)
    assert session.get(site.serve("/ok", "fine"), timeout=5).text == "fine"


# Empty tokens: a guard that is missing or comes after the token is read raises KeyError, before a request.
def test_publish_refuses_in_a_test_run_before_it_reads_a_token(cfg):
    with pytest.raises(api.PublisherError, match="from a test run"):
        api.publish(cfg, {}, {}, "Dit is de posttekst.")


def test_place_comment_refuses_in_a_test_run_before_it_reads_a_token(cfg):
    with pytest.raises(api.PublisherError, match="from a test run"):
        api.place_comment(cfg, {}, {}, "urn:li:share:1", "Hi")

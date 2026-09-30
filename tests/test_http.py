import pytest
import requests

from keiba_data.http import BlockedError, PoliteSession, RequestBudgetExceeded


class FakeResponse:
    def __init__(self, status_code, text="ok"):
        self.status_code = status_code
        self.text = text
        self.encoding = None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeRequestsSession:
    def __init__(self, responses):
        self.headers = {}
        self.responses = list(responses)
        self.calls = 0
        self.posted: list[dict | None] = []

    def get(self, url, timeout):
        return self._respond(url, None)

    def post(self, url, data, timeout):
        return self._respond(url, data)

    def _respond(self, url, data):
        self.calls += 1
        self.posted.append(data)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def make_session(responses, **kwargs):
    fake = FakeRequestsSession(responses)
    sleeps = []
    records = []
    session = PoliteSession(
        interval_sec=3.0, session=fake, sleep=sleeps.append, clock=lambda: 0.0,
        on_fetch=records.append, **kwargs,
    )
    return session, fake, sleeps, records


def test_success_sets_encoding_and_waits_between_requests():
    session, fake, sleeps, records = make_session([FakeResponse(200, "a"), FakeResponse(200, "b")])
    assert session.get_text("u1", encoding="euc-jp") == "a"
    assert session.get_text("u2", encoding="euc-jp") == "b"
    assert sleeps == [3.0]  # 2回目の前にだけ間隔を空ける
    assert [r.ok for r in records] == [True, True]


def test_retries_on_connection_error_and_server_error():
    session, fake, sleeps, _ = make_session(
        [requests.ConnectionError("boom"), FakeResponse(503), FakeResponse(200, "ok")]
    )
    assert session.get_text("u", encoding="utf-8") == "ok"
    assert fake.calls == 3
    assert session.n_requests == 3


def test_gives_up_after_max_retries():
    session, *_ = make_session([requests.Timeout("t")] * 3)
    with pytest.raises(requests.Timeout):
        session.get_text("u", encoding="utf-8")


def test_404_returns_none():
    session, *_ = make_session([FakeResponse(404)])
    assert session.get_text("u", encoding="utf-8") is None


def test_consecutive_400_is_treated_as_block():
    session, fake, *_ = make_session([FakeResponse(400)] * 3)
    with pytest.raises(BlockedError):
        session.get_text("u", encoding="utf-8")
    assert fake.calls == 3


def test_block_counter_resets_after_success():
    session, *_ = make_session(
        [FakeResponse(403), FakeResponse(403), FakeResponse(200, "ok"), FakeResponse(429), FakeResponse(200, "ok2")]
    )
    assert session.get_text("u1", encoding="utf-8") == "ok"
    assert session.get_text("u2", encoding="utf-8") == "ok2"


def test_request_budget():
    session, fake, *_ = make_session([FakeResponse(200)] * 5, max_requests=2)
    session.get_text("u1", encoding="utf-8")
    session.get_text("u2", encoding="utf-8")
    with pytest.raises(RequestBudgetExceeded):
        session.get_text("u3", encoding="utf-8")
    assert fake.calls == 2


def test_post_text_sends_the_form_and_decodes_with_the_given_encoding():
    """JRAの結果ページはPOSTでしか開けない。GETと同じ待ち方・記録の仕組みを通す。"""
    session, fake, sleeps, records = make_session([FakeResponse(200, "一覧"), FakeResponse(200, "次")])
    assert session.post_text("u", {"cname": "pw01ses…/D3"}, encoding="cp932") == "一覧"
    assert session.post_text("u", {"cname": "pw01srl…/CA"}, encoding="cp932") == "次"
    assert fake.posted == [{"cname": "pw01ses…/D3"}, {"cname": "pw01srl…/CA"}]
    assert sleeps == [3.0]  # 2回目の前に間隔を空ける
    assert [r.ok for r in records] == [True, True]


def test_post_text_retries_and_counts_like_get():
    session, fake, _sleeps, _records = make_session(
        [requests.ConnectionError("boom"), FakeResponse(200, "ok")]
    )
    assert session.post_text("u", {"cname": "x"}, encoding="cp932") == "ok"
    assert session.n_requests == 2


def test_post_text_returns_none_on_404():
    session, _fake, _sleeps, _records = make_session([FakeResponse(404)])
    assert session.post_text("u", {"cname": "x"}, encoding="cp932") is None

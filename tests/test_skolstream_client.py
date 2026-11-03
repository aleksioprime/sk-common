"""Запросы к SkolStream без redirect_uri: шлюз перед SkolStream блокирует адреса https://… в параметрах."""

from urllib.parse import parse_qs, urlparse

from sk_common.skolstream import HttpSkolstreamClient


def _client() -> HttpSkolstreamClient:
    return HttpSkolstreamClient('https://skolstream.test/', 'client', 'secret', 'token', 5)


def test_authorize_url_has_no_redirect_uri():
    url = urlparse(_client().authorize_url('state-1', 'challenge-1'))
    query = parse_qs(url.query)
    assert url.path == '/o/authorize/'
    assert 'redirect_uri' not in query and 'https://' not in url.query and 'https%3A' not in url.query
    assert query['code_challenge'] == ['challenge-1'] and query['code_challenge_method'] == ['S256']
    assert query['state'] == ['state-1'] and query['scope'] == ['read']


async def test_exchange_code_has_no_redirect_uri(monkeypatch):
    sent = {}

    class Response:
        def json(self):
            return {'access_token': 'jwt'}

    async def fake_request(method, path, **kwargs):
        sent.update(method=method, path=path, **kwargs)
        return Response()

    client = _client()
    monkeypatch.setattr(client, '_request', fake_request)
    assert await client.exchange_code('code-1', 'verifier-1') == 'jwt'
    assert sent['path'] == '/o/token/' and 'redirect_uri' not in sent['data']
    assert sent['data']['code_verifier'] == 'verifier-1'

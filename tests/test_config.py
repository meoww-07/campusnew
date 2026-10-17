import io
import json

import pytest

from app import config


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_api(monkeypatch, tunnels):
    def urlopen(url, timeout=0):
        if tunnels is None:
            raise OSError("connection refused")
        return _Resp(json.dumps({"tunnels": tunnels}).encode())
    monkeypatch.setattr("urllib.request.urlopen", urlopen)


def test_ngrok_url_detected(monkeypatch):
    _fake_api(monkeypatch, [
        {"proto": "https", "public_url": "https://other.ngrok-free.app", "config": {"addr": "http://localhost:3000"}},
        {"proto": "https", "public_url": "https://abc123.ngrok-free.app", "config": {"addr": "http://localhost:8000"}},
    ])
    assert config.ngrok_public_url() == "https://abc123.ngrok-free.app"
    assert config._base_url("ngrok", "http://localhost:8000") == "https://abc123.ngrok-free.app"


def test_plain_base_url_and_missing_tunnel(monkeypatch):
    assert config._base_url("https://campus.example.com/", "x") == "https://campus.example.com"
    assert config._base_url(None, "http://localhost:8000") == "http://localhost:8000"
    _fake_api(monkeypatch, None)
    assert config.ngrok_public_url() is None
    with pytest.raises(RuntimeError, match="ngrok http 8000"):
        config._base_url("ngrok", "http://localhost:8000")

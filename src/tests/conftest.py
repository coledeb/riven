import pytest


@pytest.fixture(autouse=True)
def fresh_torbox_health():
    """Each test starts with TorBox healthy: fake 5xx answers in one test must
    not leave it "degraded" (and holding things) for the next."""

    from program.services.downloaders.torbox_health import health

    health.__init__()
    yield
    health.__init__()

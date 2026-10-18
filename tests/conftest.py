import pytest

from app import limits


@pytest.fixture(autouse=True)
def fresh_limits():
    """Rate limits are per process: start every test with a clean slate."""
    limits.reset()
    yield

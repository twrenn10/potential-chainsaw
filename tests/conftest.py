from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def league():
    from nhl.synthetic import generate

    return generate(odds_sims=150)

import pytest


def test_ci_controlled_failure() -> None:
    pytest.fail('Controlled failure to validate the CI quality check')

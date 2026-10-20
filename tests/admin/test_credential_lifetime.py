"""Duration semantics remain in the shared credential input contract."""

import pytest
from pydantic import ValidationError

from patchouli_lib.admin.contracts import MasterProvisionAgentInput


def test_explicit_unlimited_ignores_seconds() -> None:
    value = MasterProvisionAgentInput(
        agent_name="Synthetic", never_expires=True, credential_ttl_seconds=0
    )
    assert value.never_expires is True and value.credential_ttl_seconds is None
    for seconds in (1, 31_536_000, 315_360_000):
        assert (
            MasterProvisionAgentInput(
                agent_name="Synthetic", credential_ttl_seconds=seconds
            ).credential_ttl_seconds
            == seconds
        )
    for invalid_seconds in (None, 0, -1):
        with pytest.raises(ValidationError):
            MasterProvisionAgentInput(
                agent_name="Synthetic", credential_ttl_seconds=invalid_seconds
            )

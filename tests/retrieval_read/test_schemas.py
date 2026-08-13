import pytest
from pydantic import ValidationError

from patchouli_lib.retrieval.schemas import ReadWindow


@pytest.mark.parametrize("limit", [0, 101, True, 1.5])
def test_read_window_rejects_unbounded_or_non_integer_limits(limit: object) -> None:
    with pytest.raises(ValidationError):
        ReadWindow(limit=limit)  # type: ignore[arg-type]


def test_read_window_rejects_empty_or_oversized_keys() -> None:
    with pytest.raises(ValidationError):
        ReadWindow(after_key="")
    with pytest.raises(ValidationError):
        ReadWindow(after_key="x" * 256)

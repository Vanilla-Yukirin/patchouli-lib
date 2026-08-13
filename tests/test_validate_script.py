import runpy
from collections.abc import Callable
from pathlib import Path
from typing import cast

VALIDATION_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate.py"


def test_container_validation_uses_only_a_synthetic_cursor_secret() -> None:
    namespace = runpy.run_path(str(VALIDATION_SCRIPT))
    container_validation_environment = cast(
        Callable[[dict[str, str]], dict[str, str]],
        namespace["container_validation_environment"],
    )
    original = {"PUBLIC_SENTINEL": "preserved"}

    environment = container_validation_environment(original)

    secret = environment["PATCHOULI_RETRIEVAL_CURSOR_SIGNING_SECRET"]
    assert environment["PUBLIC_SENTINEL"] == "preserved"
    assert len(secret.encode("utf-8")) >= 32
    assert secret.startswith("synthetic-validation-")
    assert original == {"PUBLIC_SENTINEL": "preserved"}

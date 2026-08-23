from importlib.metadata import EntryPoint, entry_points


def _belongs_to_server_distribution(entry_point: EntryPoint) -> bool:
    distribution = entry_point.dist
    return distribution is not None and distribution.name == "patchouli-lib"


def test_distribution_exposes_admin_password_hash_entrypoint() -> None:
    candidates = [
        entry_point
        for entry_point in entry_points(
            group="console_scripts",
            name="patchouli-admin-password",
        )
        if _belongs_to_server_distribution(entry_point)
    ]

    assert len(candidates) == 1
    assert candidates[0].value == "patchouli_lib.admin_password_cli:main"

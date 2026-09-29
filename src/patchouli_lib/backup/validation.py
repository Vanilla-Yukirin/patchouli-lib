"""Read-only structural and domain validation for experimental backup artifacts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal
from urllib.parse import quote

from pydantic import Field

from patchouli_lib.backup.errors import BackupDatabaseError
from patchouli_lib.backup.manifest import (
    FILE_SET_SCHEMA_REVISION,
    INTERMEDIATE_SCHEMA_REVISION,
    LEGACY_SCHEMA_REVISION,
    LIFECYCLE_SCHEMA_REVISION,
    OCCURRENCE_SCHEMA_REVISION,
    PREVIOUS_SCHEMA_REVISION,
    SUPPORTED_SCHEMA_REVISION,
    TAG_SCHEMA_REVISION,
)
from patchouli_lib.content.file_manifest import (
    MAX_FILE_BYTES,
    MAX_FILES_PER_PAGE,
    MAX_PAGE_BYTES,
    build_file_manifest,
)
from patchouli_lib.content.file_set_create_service import FILE_SET_CREATE_ROUTE_TEMPLATE
from patchouli_lib.content.file_set_write_service import FILE_SET_APPEND_ROUTE_TEMPLATE
from patchouli_lib.content.schemas import (
    ArchiveResponseBody,
    ContentSchema,
    OccurrenceCorrectionResponseBody,
    OpaqueId,
    PageId,
    PageLifecycleResponseBody,
    RevisionId,
)
from patchouli_lib.content.service import (
    CORRECT_OCCURRENCE_ROUTE_TEMPLATE,
    CREATE_ROUTE_TEMPLATE,
    DELETE_PAGE_ROUTE_TEMPLATE,
    RESTORE_PAGE_ROUTE_TEMPLATE,
    REVISE_ROUTE_TEMPLATE,
    legacy_page_current_etag,
    page_current_etag,
)
from patchouli_lib.identifiers import (
    canonical_utc_wire,
    page_id_registry_digest,
    page_id_timestamp_prefix,
    parse_occurrence_time,
    validate_page_id,
)
from patchouli_lib.tags.repository import normalize_tag_name

# Hashes cover every non-internal SQLite schema object at each accepted migration
# revision. SQLite-managed objects whose names start with ``sqlite_`` are the only
# exemption. An Alembic row or object name alone is therefore insufficient.
_EXPECTED_SQL_HASHES_0007: Final = {
    ("index", "ix_auth_audit_events_library_request_id"): (
        "4d13d9f0a447ee626d5796d1834202e6a65130e065dc3d9256e553dff4fe017a"
    ),
    ("index", "uq_page_identifier_registry_library_page_canonical"): (
        "263efd59e682f1a803fc4ec9db8e8897f1e42ab40138ae49a3bb046e754b0084"
    ),
    ("table", "alembic_version"): (
        "1e1ffb41bfee027fe6614ee3d494792f82bdc3e970e61cd844a37325751d9551"
    ),
    ("table", "auth_audit_events"): (
        "6f07b429c88bee095d67ea8949988064036096391732d6d03840a1460a868544"
    ),
    ("table", "auth_callers"): ("40897a2343fb473a9b0e34ae4727eb1d8f6cb8acbef1242832ba507329e5c759"),
    ("table", "auth_credentials"): (
        "7522b491a643eb1cb64b2782f7c55d23ec2037297535f64655425ceede0b0909"
    ),
    ("table", "auth_section_grants"): (
        "462c147084ce1964ca7494a71f88594dc951b067aaa9dbf94734d7d3f1f86479"
    ),
    ("table", "books"): ("0812139b964347ba630860242b7710903c9dcbf3f6c11d6933812d8c4ee5b702"),
    ("table", "idempotency_records"): (
        "a2600611d89cc74f5f4f05c9191cff1484b273203ae3ac901bec46ce368e3ebd"
    ),
    ("table", "libraries"): ("6bb11b3b4688c9845586c8ecd05981e36f2eef7ed0a5fd80376990106b2ef02f"),
    ("table", "operator_bootstrap_markers"): (
        "9e4ac714a64c125775e179eb4a6f52a0a40644a5f1f19751a98b50e67df20a5a"
    ),
    ("table", "page_id_collision_counters"): (
        "14b2fa31f0ae6d6c5242bfbee3e1f62d04d08bbdfe46c6efa310cbca918ec05d"
    ),
    ("table", "page_identifier_registry"): (
        "f00f5eda1f98d7ca627a823a8981833db71795aebef72dd169c3117080e189ef"
    ),
    ("table", "page_revision_append_guards"): (
        "f034fdb20287ef3c548294a874a22fa029da3609348dd7f9191fa382388207e6"
    ),
    ("table", "page_sources"): ("e2736429307cd28768483fb718826e686fff197abd4abc1b23aaf6f793f9e334"),
    ("table", "pages"): ("6ac4615d1122d1f5b5e7976cf865ed574086db4a0390d27643dfaaa80429d92f"),
    ("table", "revisions"): ("fc3bd5d3f0ff42d07a92ceb26477a48ce2991a7df8abedb3e821f9e6dcf529ef"),
    ("table", "revision_files"): (
        "ce67e84b09542184fc3c9a7ecfd797dec03c39cd770508e068048cc024db2109"
    ),
    ("table", "schema_metadata"): (
        "257911ed87982371b26ee44aecd9e8ee23741d25d6ab87fb1fa9b28c8e3c5b32"
    ),
    ("table", "sections"): ("0da1d13398d80ef5f57d238a462a498e81e647c575f2b4793d0e15599bed211c"),
    ("trigger", "trg_idempotency_records_immutable_update"): (
        "8ec45701f9b821e9d3336181cd59390ae8dca60352ead3d01c698a04cbe08967"
    ),
    ("trigger", "trg_idempotency_records_no_delete"): (
        "8f6803c4ac14c72813cd9c9623f3249f0042244797d899de9260349472ffa2f3"
    ),
    ("trigger", "trg_page_id_collision_counters_monotonic"): (
        "c750235b44dce0abde298ab7394d14c0e3de76ffcdb9774f836ef3e3bbc4893e"
    ),
    ("trigger", "trg_page_id_collision_counters_no_delete"): (
        "98377b594870730896d51b061a54cbe4259485ab568af86dc6418104d10541a2"
    ),
    ("trigger", "trg_page_identifier_registry_kind_on_insert"): (
        "2caddcb76a603514306b709926627cbaf38259b361ebcb711abae0fbac9f18d0"
    ),
    ("trigger", "trg_page_identifier_registry_no_delete"): (
        "d9f3e08038dbbaa0648e12df9431d45615743568d083db379f446df4620ae830"
    ),
    ("trigger", "trg_page_identifier_registry_stable"): (
        "4984e9f58cad2c80341fe07830a157b5aa16a7d9dbbff6cede168f4469f4a6ab"
    ),
    ("trigger", "trg_page_revision_append_guards_no_update"): (
        "c8dce6ff0ec7b92364a3c991aa5f5f447e8bbbaccd13103f5aa0f647eb6366cb"
    ),
    ("trigger", "trg_page_revision_append_guards_safe_delete"): (
        "f1881e422dbbb01e335b83ecf6414d56febf2fba8bd73ad5236d861d1175d6fa"
    ),
    ("trigger", "trg_pages_canonical_identifier_on_insert"): (
        "08057a2c681f9e9b3b94baf464dd81543a4fe063f89c92002365427f19aaafce"
    ),
    ("trigger", "trg_pages_clear_append_guard"): (
        "f24cfec41cbafe58644394438e85b93cd324c1b837ee7a067d17e3f7589fcc87"
    ),
    ("trigger", "trg_pages_current_revision_advance"): (
        "9753a7e581beffe804ef5a41ec4120866780e1ed3e55658a7c0074beec41c03e"
    ),
    ("trigger", "trg_pages_initial_revision_number"): (
        "bc7fe0357fee6ac8d3cdd751d9efb565948f235a277cc893f8cdf3c962fb3d25"
    ),
    ("trigger", "trg_pages_stable_identity"): (
        "7ae19e042a77bd988a7362ea88ef69e67ed2cdad60cf03b808f3ad175525087c"
    ),
    ("trigger", "trg_revisions_create_append_guard"): (
        "54b3bb78b67aaab9acefb545d8b6ce2e28238f83fe5de8612823d9b35520f30c"
    ),
    ("trigger", "trg_revisions_immutable_delete"): (
        "6278a2927aecfd931c141ee87e0a584bb6eba4b5be6c07fc403f7ddc7f7abd92"
    ),
    ("trigger", "trg_revisions_immutable_update"): (
        "b02bd56b1df874099c18d7f7f68b4a986e7ffbf7f9ced85a885418689a58b25f"
    ),
    ("trigger", "trg_revision_files_no_update"): (
        "49abb86c1ba94c56ebafba28df6cee774267be4bf058cadc9e6fe1436aa4c578"
    ),
    ("trigger", "trg_revision_files_no_delete"): (
        "2af577ba8045fbd92f173fb8baea026ba31178c52c501f3bc507df3eb07748b7"
    ),
    ("trigger", "trg_revision_files_no_replace"): (
        "1fa33224c4bcc38b03570c1215191a32c84aece8d284c6d1e68d85fa8e0623cd"
    ),
    ("trigger", "trg_revisions_mirror_content_file"): (
        "0339255ca366f97c84487473de1c0145f98516ce27308280383a4648e5869a76"
    ),
    ("trigger", "trg_revisions_sequential_insert"): (
        "bfb59191beb9acd52e5fedd6c834cd7bb51fbd49c742048c51404359730e4a1e"
    ),
}

# Generated from an actual empty SQLite database upgraded through 0008 with
# Alembic, then canonicalized with _canonical_schema_sql. The 0007 objects
# above remain byte-for-byte unchanged; only these ten objects are added.
_EXPECTED_SQL_HASHES_0008: Final = _EXPECTED_SQL_HASHES_0007 | {
    ("table", "revision_file_seals"): (
        "25c4b6e2930561429daa1c2cdce686aeb987cc5710e5d875960bc0f1eb774e2d"
    ),
    ("table", "revision_file_seal_guards"): (
        "923bdcf0a1c3d07966ee3a631d94ccc7db34fdde0958a322426230c4e4f34d9e"
    ),
    ("trigger", "trg_revision_file_seals_validate_insert"): (
        "4749500316a3010c354da4c83ed9a0693685092481b23538380f52cca570464a"
    ),
    ("trigger", "trg_revision_file_seals_no_update"): (
        "732cc0643229d14f61e3cb573403dc3208aea7fca935f1169168b676033a8f52"
    ),
    ("trigger", "trg_revision_file_seals_no_delete"): (
        "5485845f55601107699db8b2ab00ae9c9c71c763008550b2604ccf669ad62538"
    ),
    ("trigger", "trg_revision_file_seal_guards_no_update"): (
        "37b99dd027e112513f9806dbdf54c118c302b7693cb94de751e8bd94c8fd454a"
    ),
    ("trigger", "trg_revision_file_seal_guards_no_delete"): (
        "7c72e48c1258277b71357b1a615ac558194facfced33167e0928c95944cb7de9"
    ),
    ("trigger", "trg_revision_files_legacy_sealed_insert"): (
        "3ec94f52dabb94e8ec1ad31ea8f0a850daf1a2542b30258b4a7781aeba2a8f96"
    ),
    ("trigger", "trg_revision_files_auto_seal_legacy"): (
        "d7eddfe3b7e20c59a8bf6411f434e9fbb2227de49b5c8367f2354168f827781b"
    ),
    ("trigger", "trg_revisions_require_file_seal"): (
        "26b7f8a0e5db0b36352cc083979cb9b7eefa61a4eab1ba3de3e4aace92cadf9d"
    ),
}
_EXPECTED_SQL_HASHES_0009: Final = _EXPECTED_SQL_HASHES_0008 | {
    ("table", "admin_structure_audit_events"): (
        "eac48c9788b0a7105c1c398df1ffacab53506ef72f1a6cd760bfbc8181a149e8"
    ),
    ("trigger", "trg_admin_structure_audit_no_update"): (
        "64d89b8ae1eb65d79ea98a6745fc802daa99a228784ec71b808b2bd0c18094bf"
    ),
    ("trigger", "trg_admin_structure_audit_no_delete"): (
        "cc60f9851bd52e0dc775b034b64f0090e6e7eca2710c536b25ffe4583eaa2a16"
    ),
}
_EXPECTED_SQL_HASHES_0010: Final = _EXPECTED_SQL_HASHES_0009 | {
    ("index", "ix_page_tags_library_tag"): (
        "71054d2eb530e2577436d141d2c5dd167b26fb3c4644a3c33dd41656d7f8ac43"
    ),
    ("table", "page_tags"): ("f68997a2d94d863f237ed3cd958ca4a47d3f4ea1cdfc381fadfb6e789947c44b"),
    ("table", "tags"): ("994997e390e79449ed2f23caf2281fce465c1855213cf6cb289953a878ccdaf8"),
}
_EXPECTED_SQL_HASHES_0011: Final = _EXPECTED_SQL_HASHES_0010 | {
    ("table", "page_occurrence_correction_guards"): (
        "b5faf884e8022c7874b80108563edc76ed32269abf609b05b97de95ff83752ab"
    ),
    ("table", "page_occurrence_corrections"): (
        "1ca1798efe1ccc6964d272e589e4c8465b4b02fe73625bdfbe89c7c3adb58fe6"
    ),
    ("trigger", "trg_page_occurrence_corrections_no_delete"): (
        "3a66a805ccc45ccbe1b1f535eba42e9b4798fc34675d639228f18dea9248dc6b"
    ),
    ("trigger", "trg_page_occurrence_corrections_no_update"): (
        "3c03990ed65738714f5aee5bdae6871bf376ee392b63d6d0d23d508cad6a1967"
    ),
    ("trigger", "trg_page_occurrence_corrections_validate_insert"): (
        "4c389a821f8307c48b6b3eaa9fa8c93814c7f692516700e16867f5942765697a"
    ),
    ("trigger", "trg_page_occurrence_guards_no_update"): (
        "d9464288878e381c9ad33bc1b1c5adbcd738063540aa1c0faf3f8875e3519ceb"
    ),
    ("trigger", "trg_page_occurrence_guards_safe_delete"): (
        "11bb4f88cf8213e9217b7b25be63590751ae314359cc784e7b93b0dfca0679e9"
    ),
    ("trigger", "trg_page_occurrence_guards_validate_insert"): (
        "b694bbf7a0e865bf77583b4e01932b1dfa2f88971faa933924e0cc8d403a796e"
    ),
    ("trigger", "trg_pages_occurrence_record"): (
        "fda54a8097c56827ad9151fb260463f55ba5b39eb5dcb3ef80466e8228de4d58"
    ),
    ("trigger", "trg_pages_occurrence_require_guard"): (
        "8390ecb33e63768a37c34af9ea735c51572f24f20f521f58898efc140909fa0a"
    ),
    ("trigger", "trg_pages_stable_identity"): (
        "6e3ebd7d5e4010cd3cfba1428daf8cc9e0fe9072f11f81562ae9995bc27d627c"
    ),
    ("trigger", "trg_pages_updated_at_monotonic"): (
        "78b91ef08c6ada45dda3e2871aa0c395bfa827d437587e0811218f3b353922ad"
    ),
}
_EXPECTED_SQL_HASHES_0012: Final = _EXPECTED_SQL_HASHES_0011 | {
    ("table", "page_lifecycle_events"): (
        "a6fae4ae517828eb9c82a2c5d0a85a61ce8c11fee311c4ebf5195548d28aac4f"
    ),
    ("table", "page_lifecycle_guards"): (
        "5c184747f7ba506eabe0258c95c14e97910b1e078d9a61c4c1e257ccf069c8c3"
    ),
    ("trigger", "trg_page_lifecycle_events_no_delete"): (
        "e97e45c33aa019c9de8918a31e2ccdc1de352100f4744c4841f707b75cfd8da6"
    ),
    ("trigger", "trg_page_lifecycle_events_no_update"): (
        "d3ef66ea934acc122740fa3b4a0f313695fcfffe5ed81f46cdc3ff70ad63692d"
    ),
    ("trigger", "trg_page_lifecycle_events_validate_insert"): (
        "6e20fa7fdd60de2730298a871f78f2e8e3f31a3d4f563cab0335909a15ec5166"
    ),
    ("trigger", "trg_page_lifecycle_guards_no_update"): (
        "a4f98259cd17461d4de9598f6ef8b02cc2fe8f9bbdaa9ee85334adced9b04d45"
    ),
    ("trigger", "trg_page_lifecycle_guards_safe_delete"): (
        "52fb5663affdbd59ab11fa746a1f2bb854c9d60130861a337a88b3cded467ed2"
    ),
    ("trigger", "trg_page_lifecycle_guards_validate_insert"): (
        "b4df4a7e5004d375f682f7641ebd6d6894129317b4cb3c26370a4de3089fa9c5"
    ),
    ("trigger", "trg_pages_lifecycle_initial_live"): (
        "246e23249ddc26de5624ff0c7927e99ea394f7bf0be5d4c16ce46249b43f1e49"
    ),
    ("trigger", "trg_pages_lifecycle_no_content_while_deleted"): (
        "19c5e7507f805534849e722f26e4eb9a3cda7cb3b6d5598b69115e830101283b"
    ),
    ("trigger", "trg_pages_lifecycle_record"): (
        "feb33d1a28741f5ebee3987b1f41ffec4a5c5d85622b65ac407e73a8dba98722"
    ),
    ("trigger", "trg_pages_lifecycle_require_guard"): (
        "8ddf19a61f02d73470377dfdba12c4a50dbbdbf21d5af58c1ff71fb5726289fb"
    ),
}
_EXPECTED_SQL_HASHES_0013: Final = _EXPECTED_SQL_HASHES_0012 | {
    # Produced from an empty database upgraded through 0013 with Alembic,
    # using the same canonical SQL normalization as every earlier head.
    ("table", "revision_file_sets"): (
        "7b6a8edbf33ec75be858e34467af0a5b66b40857e933730c6e043edfbf791816"
    ),
    ("table", "revisions"): ("71105002c4fe6bcc2e2395a5275ebafe0f82a9c5d8af64c43149eda8470eb0b2"),
    ("trigger", "trg_revision_file_seals_validate_insert"): (
        "dcf7e778802878db2283de905f7686633b5348b1d36da61128d3303937fdde83"
    ),
    ("trigger", "trg_revision_file_sets_no_delete"): (
        "f153580fb563010c427baf200993a66a66d983fd8cc0ada3b2a4f5d5593a297f"
    ),
    ("trigger", "trg_revision_file_sets_no_replace"): (
        "e4a888f9254ce24209a483b19b92eb87d7fa0e031aaf9cfbb123816ea393cff3"
    ),
    ("trigger", "trg_revision_file_sets_no_update"): (
        "8b41aeef4ba4e11d5a95135d75d222a3e043f3b2b4795e7a9e4821b38662abe5"
    ),
    ("trigger", "trg_revision_file_sets_validate_insert"): (
        "42af79a76543598aecd1756d1f2912e0ba019e78515f0d6b14f8849883f0e5aa"
    ),
    ("trigger", "trg_revision_files_auto_seal_legacy"): (
        "e3b74e733993c094a77c2b79ea925b3f21ea4f2973a7815fe8e43114b15329d6"
    ),
    ("trigger", "trg_revision_files_legacy_sealed_insert"): (
        "2a68b99f4f38eaec0a4700097382edecfac806a58bff6fe1995baa2572a0e579"
    ),
    ("trigger", "trg_revisions_mirror_content_file"): (
        "e44829a4bfd0ea7342a066c9f711bdb3fdac9c458be58b4829b24adf861fce36"
    ),
}
_EXPECTED_SQL_HASHES_0014: Final = _EXPECTED_SQL_HASHES_0013 | {
    # Generated from an empty Alembic 0014 database with _canonical_schema_sql.
    ("index", "ix_auth_credential_library_grants_target_action"): (
        "150e3351cc94c75358369213a016a2b3828501093677a22dfc0b2280a5bc0dd1"
    ),
    ("table", "auth_credential_library_grants"): (
        "ee902f27eaeeafd61f4b1aab7ae464fe9f157ce0e3e965d1f8b8f7cc0b2c5b55"
    ),
    ("table", "auth_credential_library_policies"): (
        "7de28d8ea6fdc62a429f2e323ddfa96e3ccf17575460551a818111de2798b2b8"
    ),
    ("trigger", "trg_auth_credential_library_policies_agent_only"): (
        "013036af08830d969d800144e401545efbedb5475bb9f93bff90e45d0b596649"
    ),
    ("trigger", "trg_auth_credential_library_policies_immutable_delete"): (
        "13c3660c0a9ae4599dab04576256a97dda49a2ac147a6308568cbdec7c50841b"
    ),
    ("trigger", "trg_auth_credential_library_policies_immutable_update"): (
        "4e565589ff5232218294b3b5a8242a9b99d04fe242a78b4cedb0e1297ffb32d5"
    ),
}
_EXPECTED_SQL_HASHES_BY_REVISION: Final = {
    LEGACY_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0007,
    PREVIOUS_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0008,
    INTERMEDIATE_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0009,
    TAG_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0010,
    OCCURRENCE_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0011,
    LIFECYCLE_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0012,
    FILE_SET_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0013,
    SUPPORTED_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0014,
}
_FILE_SET_REVISIONS: Final = frozenset({FILE_SET_SCHEMA_REVISION, SUPPORTED_SCHEMA_REVISION})
_LIFECYCLE_REVISIONS: Final = _FILE_SET_REVISIONS | {LIFECYCLE_SCHEMA_REVISION}
_OCCURRENCE_REVISIONS: Final = _LIFECYCLE_REVISIONS | {OCCURRENCE_SCHEMA_REVISION}


@dataclass(frozen=True, slots=True)
class DatabaseValidationReport:
    """Non-sensitive facts proven for a self-contained database artifact."""

    schema_revision: str
    sqlite_version: str
    artifact_journal_mode: str


def _read_only_uri(path: Path) -> str:
    encoded = quote(path.as_posix(), safe="/:")
    return f"file:{encoded}?mode=ro&immutable=1"


def _one_integer(connection: sqlite3.Connection, query: str) -> int:
    row = connection.execute(query).fetchone()
    if row is None or type(row[0]) is not int:
        raise BackupDatabaseError
    return row[0]


def _canonical_schema_sql(object_type: str, sql: str) -> str:
    normalized = " ".join(sql.split())
    if object_type != "table":
        return normalized
    opening = normalized.find("(")
    if opening < 0 or not normalized.endswith(")"):
        raise BackupDatabaseError
    prefix = normalized[:opening].rstrip()
    body = normalized[opening + 1 : -1]
    clauses: list[str] = []
    start = 0
    depth = 0
    quote_character: str | None = None
    index = 0
    while index < len(body):
        character = body[index]
        if quote_character is not None:
            if character == quote_character:
                if index + 1 < len(body) and body[index + 1] == quote_character:
                    index += 1
                else:
                    quote_character = None
        elif character in {"'", '"'}:
            quote_character = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                raise BackupDatabaseError
        elif character == "," and depth == 0:
            clauses.append(body[start:index].strip())
            start = index + 1
        index += 1
    if quote_character is not None or depth != 0:
        raise BackupDatabaseError
    clauses.append(body[start:].strip())
    if not clauses or any(not clause for clause in clauses):
        raise BackupDatabaseError
    columns = [clause for clause in clauses if not clause.startswith("CONSTRAINT ")]
    constraints = sorted(clause for clause in clauses if clause.startswith("CONSTRAINT "))
    if len(columns) + len(constraints) != len(clauses):
        raise BackupDatabaseError
    return f"{prefix} ({', '.join((*columns, *constraints))})"


def _require_schema(connection: sqlite3.Connection, schema_revision: str) -> str:
    if type(schema_revision) is not str or schema_revision not in _EXPECTED_SQL_HASHES_BY_REVISION:
        raise BackupDatabaseError
    rows = connection.execute(
        "SELECT type, name, sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*'"
    )
    observed: dict[tuple[str, str], str] = {}
    for object_type, name, sql in rows:
        if not all(isinstance(value, str) for value in (object_type, name, sql)):
            raise BackupDatabaseError
        normalized = _canonical_schema_sql(object_type, sql)
        observed[(object_type, name)] = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    if observed != _EXPECTED_SQL_HASHES_BY_REVISION[schema_revision]:
        raise BackupDatabaseError

    revisions = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    if revisions != [(schema_revision,)]:
        raise BackupDatabaseError
    return schema_revision


def _require_sqlite_integrity(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise BackupDatabaseError
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise BackupDatabaseError


def _require_page_graph(connection: sqlite3.Connection, schema_revision: str) -> None:
    if _one_integer(connection, "SELECT count(*) FROM page_revision_append_guards") != 0:
        raise BackupDatabaseError

    invalid_current = _one_integer(
        connection,
        "SELECT count(*) FROM pages AS p WHERE NOT EXISTS ("
        "SELECT 1 FROM revisions AS current "
        "WHERE current.library_id = p.library_id AND current.page_uid = p.page_uid "
        "AND current.revision_id = p.current_revision_id "
        "AND current.revision_number = p.current_revision_number) "
        "OR (SELECT min(r.revision_number) FROM revisions AS r "
        "WHERE r.library_id = p.library_id AND r.page_uid = p.page_uid) != 1 "
        "OR (SELECT max(r.revision_number) FROM revisions AS r "
        "WHERE r.library_id = p.library_id AND r.page_uid = p.page_uid) "
        "!= p.current_revision_number "
        "OR (SELECT count(*) FROM revisions AS r "
        "WHERE r.library_id = p.library_id AND r.page_uid = p.page_uid) "
        "!= p.current_revision_number",
    )
    if invalid_current:
        raise BackupDatabaseError

    for content, size, digest in connection.execute(
        "SELECT content_md, content_size_bytes, content_sha256 FROM revisions"
    ):
        if schema_revision in _FILE_SET_REVISIONS and (content, size, digest) == (
            None,
            None,
            None,
        ):
            continue
        if type(content) is not bytes or type(size) is not int or type(digest) is not bytes:
            raise BackupDatabaseError
        try:
            content.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise BackupDatabaseError from None
        if b"\x00" in content or len(content) != size or hashlib.sha256(content).digest() != digest:
            raise BackupDatabaseError

    invalid_sources = _one_integer(
        connection,
        "SELECT count(*) FROM page_sources AS s WHERE NOT EXISTS ("
        "SELECT 1 FROM revisions AS r WHERE r.library_id = s.library_id "
        "AND r.page_uid = s.page_uid AND r.revision_id = s.revision_id "
        "AND r.revision_number = s.revision_number)",
    )
    if invalid_sources:
        raise BackupDatabaseError
    missing_sources = _one_integer(
        connection,
        "SELECT count(*) FROM revisions AS r WHERE NOT EXISTS ("
        "SELECT 1 FROM page_sources AS s WHERE s.library_id = r.library_id "
        "AND s.page_uid = r.page_uid AND s.revision_id = r.revision_id "
        "AND s.revision_number = r.revision_number)",
    )
    if missing_sources:
        raise BackupDatabaseError

    _require_revision_files(connection, schema_revision)
    if schema_revision != LEGACY_SCHEMA_REVISION:
        _require_revision_seals(connection)

    occurrence_initials = _require_occurrence_graph(connection, schema_revision)
    identifiers_by_page: dict[tuple[str, bytes], list[tuple[str, str]]] = {}
    for library_id, digest, text, kind, page_uid in connection.execute(
        "SELECT library_id, identifier_digest, identifier_text, identifier_kind, page_uid "
        "FROM page_identifier_registry"
    ):
        if not isinstance(library_id, str) or type(page_uid) is not bytes:
            raise BackupDatabaseError
        if type(digest) is not bytes or not isinstance(text, str) or not isinstance(kind, str):
            raise BackupDatabaseError
        try:
            validate_page_id(text)
            expected_digest = page_id_registry_digest(text)
        except ValueError:
            raise BackupDatabaseError from None
        if digest != expected_digest:
            raise BackupDatabaseError
        identifiers_by_page.setdefault((library_id, page_uid), []).append((text, kind))

    page_counter_keys: set[tuple[str, str, int, str]] = set()
    for (
        library_id,
        page_uid,
        page_id,
        scheme,
        timestamp,
        slug,
        ordinal,
        occurred_at,
    ) in connection.execute(
        "SELECT library_id, page_uid, page_id, id_scheme, id_timestamp_micros, "
        "base_slug, collision_ordinal, occurred_at FROM pages"
    ):
        if (
            not isinstance(library_id, str)
            or type(page_uid) is not bytes
            or not isinstance(page_id, str)
            or not isinstance(scheme, str)
            or type(timestamp) is not int
            or not isinstance(slug, str)
            or type(ordinal) is not int
            or type(occurred_at) is not int
        ):
            raise BackupDatabaseError
        suffix = "" if ordinal == 1 else f"-{ordinal}"
        try:
            expected_page_id = f"{page_id_timestamp_prefix(timestamp)}-{slug}{suffix}"
            validate_page_id(expected_page_id)
        except ValueError:
            raise BackupDatabaseError from None
        if (
            scheme != "page-v1"
            or page_id != expected_page_id
            or timestamp
            != (occurrence_initials.get((library_id, page_uid), occurred_at) // 1000) * 1000
        ):
            raise BackupDatabaseError
        identifiers = identifiers_by_page.get((library_id, page_uid), [])
        if identifiers.count((page_id, "canonical")) != 1:
            raise BackupDatabaseError
        page_counter_keys.add((library_id, scheme, timestamp, slug))

    observed_counter_keys: set[tuple[str, str, int, str]] = set()
    for library_id, scheme, timestamp, slug, next_ordinal in connection.execute(
        "SELECT library_id, id_scheme, id_timestamp_micros, base_slug, next_ordinal "
        "FROM page_id_collision_counters"
    ):
        if (
            not isinstance(library_id, str)
            or not isinstance(scheme, str)
            or type(timestamp) is not int
            or not isinstance(slug, str)
            or type(next_ordinal) is not int
        ):
            raise BackupDatabaseError
        key = (library_id, scheme, timestamp, slug)
        observed_counter_keys.add(key)
        row = connection.execute(
            "SELECT max(collision_ordinal) FROM pages WHERE library_id = ? "
            "AND id_scheme = ? AND id_timestamp_micros = ? AND base_slug = ?",
            key,
        ).fetchone()
        if row is None or type(row[0]) is not int or next_ordinal <= row[0]:
            raise BackupDatabaseError
    if page_counter_keys != observed_counter_keys:
        raise BackupDatabaseError


def _require_occurrence_graph(
    connection: sqlite3.Connection, schema_revision: str
) -> dict[tuple[str, bytes], int]:
    """Validate the complete correction chain and return each Page's ID time."""

    if schema_revision not in _OCCURRENCE_REVISIONS:
        return {}
    if _one_integer(connection, "SELECT count(*) FROM page_occurrence_correction_guards"):
        raise BackupDatabaseError
    page_rows = connection.execute(
        "SELECT library_id, page_uid, occurred_at, current_revision_number, "
        "created_at, updated_at FROM pages"
    )
    pages = {
        (library_id, page_uid): (occurred_at, current_revision_number, created_at, updated_at)
        for (
            library_id,
            page_uid,
            occurred_at,
            current_revision_number,
            created_at,
            updated_at,
        ) in page_rows
    }
    revision_created = {
        (library_id, page_uid, number): created_at
        for library_id, page_uid, number, created_at in connection.execute(
            "SELECT library_id, page_uid, revision_number, created_at FROM revisions"
        )
    }
    chains: dict[tuple[str, bytes], list[tuple[int, int, int, int, str, int]]] = {}
    for (
        library_id,
        page_uid,
        sequence,
        old,
        new,
        at_revision,
        actor,
        corrected_at,
    ) in connection.execute(
        "SELECT library_id, page_uid, sequence, old_occurred_at, new_occurred_at, "
        "at_revision_number, actor_caller_id, corrected_at "
        "FROM page_occurrence_corrections ORDER BY library_id, page_uid, sequence"
    ):
        key = (library_id, page_uid)
        if (
            key not in pages
            or type(sequence) is not int
            or type(old) is not int
            or type(new) is not int
            or type(at_revision) is not int
            or type(actor) is not str
            or type(corrected_at) is not int
        ):
            raise BackupDatabaseError
        chains.setdefault(key, []).append((sequence, old, new, at_revision, actor, corrected_at))

    initials: dict[tuple[str, bytes], int] = {}
    for key, (current, current_revision, created_at, updated_at) in pages.items():
        if (
            type(key[0]) is not str
            or type(key[1]) is not bytes
            or type(current) is not int
            or type(current_revision) is not int
            or type(created_at) is not int
            or type(updated_at) is not int
        ):
            raise BackupDatabaseError
        chain = chains.get(key, [])
        value = chain[0][1] if chain else current
        initials[key] = value
        prior_revision = 1
        prior_time = -1
        for expected_sequence, (sequence, old, new, at_revision, _actor, corrected_at) in enumerate(
            chain, start=1
        ):
            next_revision_time = (
                revision_created.get((*key, at_revision + 1))
                if at_revision < current_revision
                else None
            )
            if (
                sequence != expected_sequence
                or old != value
                or new == old
                or not prior_revision <= at_revision <= current_revision
                or corrected_at <= prior_time
                or corrected_at < created_at
                or type(revision_created.get((*key, at_revision))) is not int
                or corrected_at < revision_created[(*key, at_revision)]
                or (
                    at_revision < current_revision
                    and (type(next_revision_time) is not int or corrected_at >= next_revision_time)
                )
                or corrected_at > updated_at
            ):
                raise BackupDatabaseError
            value = new
            prior_revision = at_revision
            prior_time = corrected_at
        if value != current:
            raise BackupDatabaseError
    return initials


def _require_lifecycle_graph(connection: sqlite3.Connection) -> None:
    """Rebuild each 0012 Page clock across Revisions, corrections, and trash events."""

    if _one_integer(connection, "SELECT count(*) FROM page_lifecycle_guards"):
        raise BackupDatabaseError

    pages: dict[tuple[str, bytes], tuple[str, str, str, int, int | None, int, int, int]] = {}
    for row in connection.execute(
        "SELECT library_id, page_uid, section_id, page_id, page_type, occurred_at, "
        "deleted_at, created_at, updated_at, current_revision_number FROM pages"
    ):
        (
            library_id,
            page_uid,
            section_id,
            page_id,
            page_type,
            occurred_at,
            deleted_at,
            created_at,
            updated_at,
            current_revision_number,
        ) = row
        if (
            type(library_id) is not str
            or type(page_uid) is not bytes
            or type(section_id) is not str
            or type(page_id) is not str
            or type(page_type) is not str
            or type(occurred_at) is not int
            or (deleted_at is not None and type(deleted_at) is not int)
            or type(created_at) is not int
            or type(updated_at) is not int
            or type(current_revision_number) is not int
        ):
            raise BackupDatabaseError
        pages[(library_id, page_uid)] = (
            section_id,
            page_id,
            page_type,
            occurred_at,
            deleted_at,
            created_at,
            updated_at,
            current_revision_number,
        )

    # A Page's updated_at changes only for a Revision, an occurrence correction,
    # or a lifecycle transition. Replay all three on a single logical clock.
    first_revisions: dict[tuple[str, bytes], tuple[str, int]] = {}
    operations: dict[tuple[str, bytes], list[tuple[int, str, tuple[object, ...]]]] = {}
    for library_id, page_uid, number, revision_id, created_at in connection.execute(
        "SELECT library_id, page_uid, revision_number, revision_id, created_at "
        "FROM revisions ORDER BY library_id, page_uid, revision_number"
    ):
        key = (library_id, page_uid)
        if (
            key not in pages
            or type(number) is not int
            or type(revision_id) is not str
            or type(created_at) is not int
        ):
            raise BackupDatabaseError
        if number == 1:
            first_revisions[key] = (revision_id, created_at)
        else:
            operations.setdefault(key, []).append((created_at, "revision", (number, revision_id)))

    first_occurrences: dict[tuple[str, bytes], int] = {}
    for library_id, page_uid, sequence, old, new, number, corrected_at in connection.execute(
        "SELECT library_id, page_uid, sequence, old_occurred_at, new_occurred_at, "
        "at_revision_number, corrected_at FROM page_occurrence_corrections "
        "ORDER BY library_id, page_uid, sequence"
    ):
        key = (library_id, page_uid)
        if (
            key not in pages
            or type(sequence) is not int
            or type(old) is not int
            or type(new) is not int
            or type(number) is not int
            or type(corrected_at) is not int
        ):
            raise BackupDatabaseError
        first_occurrences.setdefault(key, old)
        operations.setdefault(key, []).append(
            (corrected_at, "correction", (sequence, old, new, number))
        )

    for row in connection.execute(
        "SELECT library_id, page_uid, sequence, action, section_id, old_deleted_at, "
        "old_updated_at, changed_at, at_revision_number, occurred_at_at_event, "
        "actor_caller_id, request_id FROM page_lifecycle_events "
        "ORDER BY library_id, page_uid, sequence"
    ):
        (
            library_id,
            page_uid,
            sequence,
            action,
            section_id,
            old_deleted_at,
            old_updated_at,
            changed_at,
            number,
            occurrence,
            actor,
            request_id,
        ) = row
        key = (library_id, page_uid)
        if (
            key not in pages
            or type(sequence) is not int
            or action not in {"delete", "restore"}
            or type(section_id) is not str
            or (old_deleted_at is not None and type(old_deleted_at) is not int)
            or type(old_updated_at) is not int
            or type(changed_at) is not int
            or type(number) is not int
            or type(occurrence) is not int
            or type(actor) is not str
            or type(request_id) is not str
        ):
            raise BackupDatabaseError
        try:
            timestamp = canonical_utc_wire(changed_at)
        except ValueError:
            raise BackupDatabaseError from None
        method, route = (
            ("DELETE", DELETE_PAGE_ROUTE_TEMPLATE)
            if action == "delete"
            else ("POST", RESTORE_PAGE_ROUTE_TEMPLATE)
        )
        replays = connection.execute(
            "SELECT response_body FROM idempotency_records WHERE library_id = ? "
            "AND caller_id = ? AND method = ? AND route_template = ? "
            "AND original_request_id = ? AND original_request_timestamp = ?",
            (library_id, actor, method, route, request_id, timestamp),
        ).fetchall()
        if len(replays) != 1 or type(replays[0][0]) is not bytes:
            raise BackupDatabaseError
        try:
            replay_body = PageLifecycleResponseBody.model_validate_json(replays[0][0])
        except ValueError:
            raise BackupDatabaseError from None
        if replay_body.section_id != section_id or replay_body.page_id != pages[key][1]:
            raise BackupDatabaseError
        operations.setdefault(key, []).append(
            (
                changed_at,
                "lifecycle",
                (
                    sequence,
                    action,
                    section_id,
                    old_deleted_at,
                    old_updated_at,
                    number,
                    occurrence,
                ),
            )
        )

    for key, (
        section_id,
        _page_id,
        page_type,
        final_occurrence,
        final_deleted_at,
        created_at,
        updated_at,
        final_revision,
    ) in pages.items():
        first_revision = first_revisions.get(key)
        if first_revision is None or first_revision[1] != created_at:
            raise BackupDatabaseError
        revision_number = 1
        occurrence = first_occurrences.get(key, final_occurrence)
        replayed_deleted_at: int | None = None
        prior_time = created_at
        correction_sequence = 0
        lifecycle_sequence = 0
        for time, kind, values in sorted(operations.get(key, []), key=lambda item: item[0]):
            if time <= prior_time:
                raise BackupDatabaseError
            if kind == "revision":
                number, _revision_id = values
                if replayed_deleted_at is not None or number != revision_number + 1:
                    raise BackupDatabaseError
                revision_number = number
            elif kind == "correction":
                sequence, old, new, number = values
                if (
                    replayed_deleted_at is not None
                    or sequence != correction_sequence + 1
                    or number != revision_number
                    or old != occurrence
                    or new == old
                ):
                    raise BackupDatabaseError
                correction_sequence = sequence
                occurrence = new
            else:
                sequence, action, event_section, old_deleted, old_updated, number, event_time = (
                    values
                )
                if (
                    page_type != "archive"
                    or sequence != lifecycle_sequence + 1
                    or event_section != section_id
                    or old_deleted != replayed_deleted_at
                    or old_updated != prior_time
                    or number != revision_number
                    or event_time != occurrence
                    or (action == "delete") != (replayed_deleted_at is None)
                ):
                    raise BackupDatabaseError
                lifecycle_sequence = sequence
                replayed_deleted_at = time if action == "delete" else None
            prior_time = time
        if (
            updated_at != prior_time
            or final_deleted_at != replayed_deleted_at
            or final_revision != revision_number
            or final_occurrence != occurrence
        ):
            raise BackupDatabaseError


def _require_revision_files(connection: sqlite3.Connection, schema_revision: str) -> None:
    """Check every file and the revision-specific complete snapshot policy."""

    if schema_revision in _FILE_SET_REVISIONS:
        _require_manifested_revision_files(connection)
        return

    revisions = connection.execute(
        "SELECT library_id, page_uid, revision_id, revision_number, "
        "content_md, content_size_bytes, content_sha256 FROM revisions"
    )
    for (
        library_id,
        page_uid,
        revision_id,
        revision_number,
        legacy,
        legacy_size,
        legacy_sha,
    ) in revisions:
        if (
            not isinstance(library_id, str)
            or type(page_uid) is not bytes
            or not isinstance(revision_id, str)
            or type(revision_number) is not int
            or type(legacy) is not bytes
            or type(legacy_size) is not int
            or type(legacy_sha) is not bytes
        ):
            raise BackupDatabaseError

        files: list[tuple[str, bytes]] = []
        total_size = 0
        found_markdown = False
        rows = connection.execute(
            "SELECT filename, content_bytes, size_bytes, content_sha256 "
            "FROM revision_files WHERE library_id = ? AND page_uid = ? "
            "AND revision_id = ? AND revision_number = ? ORDER BY filename",
            (library_id, page_uid, revision_id, revision_number),
        )
        for name, content, size, digest in rows:
            if (
                not isinstance(name, str)
                or type(content) is not bytes
                or type(size) is not int
                or type(digest) is not bytes
                or len(files) >= MAX_FILES_PER_PAGE
                or len(content) > MAX_FILE_BYTES
                or total_size + len(content) > MAX_PAGE_BYTES
                or size != len(content)
                or digest != hashlib.sha256(content).digest()
            ):
                raise BackupDatabaseError
            if name == "content.md":
                if (
                    found_markdown
                    or content != legacy
                    or size != legacy_size
                    or digest != legacy_sha
                ):
                    raise BackupDatabaseError
                found_markdown = True
            total_size += len(content)
            files.append((name, content))
        if not found_markdown:
            raise BackupDatabaseError
        if schema_revision != LEGACY_SCHEMA_REVISION and (
            len(files) != 1 or files[0][0] != "content.md"
        ):
            raise BackupDatabaseError
        try:
            build_file_manifest(files)
        except (TypeError, ValueError, OverflowError, UnicodeError):
            raise BackupDatabaseError from None


def _require_manifested_revision_files(connection: sqlite3.Connection) -> None:
    """Verify exact 0013 file-set rows, bytes, hashes and complete digests."""

    revision_count = _one_integer(connection, "SELECT count(*) FROM revisions")
    manifest_count = _one_integer(connection, "SELECT count(*) FROM revision_file_sets")
    if revision_count != manifest_count:
        raise BackupDatabaseError

    revisions = connection.execute(
        "SELECT r.library_id, r.page_uid, r.revision_id, r.revision_number, "
        "r.content_md, r.content_size_bytes, r.content_sha256, "
        "m.storage_format, m.file_count, m.total_size_bytes, m.snapshot_sha256 "
        "FROM revisions AS r LEFT JOIN revision_file_sets AS m "
        "ON m.library_id = r.library_id AND m.page_uid = r.page_uid "
        "AND m.revision_id = r.revision_id AND m.revision_number = r.revision_number"
    )
    for (
        library_id,
        page_uid,
        revision_id,
        revision_number,
        legacy,
        legacy_size,
        legacy_sha,
        storage_format,
        file_count,
        total_size,
        snapshot_sha,
    ) in revisions:
        if (
            type(library_id) is not str
            or type(page_uid) is not bytes
            or type(revision_id) is not str
            or type(revision_number) is not int
            or storage_format not in {"legacy_markdown", "file_set_v1"}
            or type(file_count) is not int
            or type(total_size) is not int
        ):
            raise BackupDatabaseError

        files: list[tuple[str, bytes]] = []
        observed_total_size = 0
        rows = connection.execute(
            "SELECT filename, content_bytes, size_bytes, content_sha256 "
            "FROM revision_files WHERE library_id = ? AND page_uid = ? "
            "AND revision_id = ? AND revision_number = ? ORDER BY filename",
            (library_id, page_uid, revision_id, revision_number),
        )
        for name, content, size, digest in rows:
            if (
                type(name) is not str
                or type(content) is not bytes
                or type(size) is not int
                or type(digest) is not bytes
                or len(files) >= MAX_FILES_PER_PAGE
                or len(content) > MAX_FILE_BYTES
                or observed_total_size + len(content) > MAX_PAGE_BYTES
                or size != len(content)
                or digest != hashlib.sha256(content).digest()
            ):
                raise BackupDatabaseError
            observed_total_size += len(content)
            files.append((name, content))
        try:
            canonical = build_file_manifest(files)
        except (TypeError, ValueError, OverflowError, UnicodeError):
            raise BackupDatabaseError from None
        if [(entry.name, entry.content) for entry in canonical.files] != files:
            # The builder normalizes input names for creation. Stored names
            # themselves must already be canonical, not merely normalizable.
            raise BackupDatabaseError
        if file_count != len(canonical.files) or total_size != canonical.total_size_bytes:
            raise BackupDatabaseError
        if storage_format == "legacy_markdown":
            if (
                type(legacy) is not bytes
                or type(legacy_size) is not int
                or type(legacy_sha) is not bytes
                or len(canonical.files) != 1
                or canonical.files[0].name != "content.md"
                or canonical.files[0].content != legacy
                or canonical.files[0].content_size_bytes != legacy_size
                or canonical.files[0].content_sha256 != legacy_sha
                or snapshot_sha is not None
            ):
                raise BackupDatabaseError
        elif (
            legacy is not None
            or legacy_size is not None
            or legacy_sha is not None
            or type(snapshot_sha) is not bytes
            or snapshot_sha != canonical.snapshot_sha256
        ):
            raise BackupDatabaseError


def _require_revision_seals(connection: sqlite3.Connection) -> None:
    """Every 0008 Revision must have precisely its exact seal and guard."""

    missing = _one_integer(
        connection,
        "SELECT count(*) FROM revisions AS r WHERE NOT EXISTS ("
        "SELECT 1 FROM revision_file_seals AS s WHERE s.library_id = r.library_id "
        "AND s.page_uid = r.page_uid AND s.revision_id = r.revision_id "
        "AND s.revision_number = r.revision_number) OR NOT EXISTS ("
        "SELECT 1 FROM revision_file_seal_guards AS g WHERE g.library_id = r.library_id "
        "AND g.page_uid = r.page_uid AND g.revision_id = r.revision_id "
        "AND g.revision_number = r.revision_number)",
    )
    if missing:
        raise BackupDatabaseError
    # The exact composite foreign keys catch orphan markers; these counts also
    # fail closed if SQLite FK enforcement was bypassed before backup creation.
    revision_count = _one_integer(connection, "SELECT count(*) FROM revisions")
    if (
        _one_integer(connection, "SELECT count(*) FROM revision_file_seals") != revision_count
        or _one_integer(connection, "SELECT count(*) FROM revision_file_seal_guards")
        != revision_count
    ):
        raise BackupDatabaseError


def _require_auth_graph(connection: sqlite3.Connection, schema_revision: str) -> None:
    invalid_bootstrap = _one_integer(
        connection,
        "SELECT count(*) FROM operator_bootstrap_markers AS m "
        "LEFT JOIN auth_callers AS c ON c.id = m.operator_caller_id "
        "AND c.library_id = m.library_id "
        "LEFT JOIN auth_credentials AS k ON k.id = m.initial_credential_id "
        "AND k.caller_id = m.operator_caller_id AND k.library_id = m.library_id "
        "WHERE c.id IS NULL OR c.kind != 'operator' OR k.id IS NULL",
    )
    if invalid_bootstrap:
        raise BackupDatabaseError

    invalid_grants = _one_integer(
        connection,
        "SELECT count(*) FROM auth_section_grants AS g "
        "JOIN auth_callers AS c ON c.id = g.caller_id AND c.library_id = g.library_id "
        "WHERE c.kind != 'agent'",
    )
    if invalid_grants:
        raise BackupDatabaseError

    if schema_revision == SUPPORTED_SCHEMA_REVISION:
        invalid_library_policies = _one_integer(
            connection,
            "SELECT count(*) FROM auth_credential_library_policies AS p "
            "LEFT JOIN auth_credentials AS k ON k.id = p.credential_id "
            "AND k.caller_id = p.caller_id AND k.library_id = p.home_library_id "
            "LEFT JOIN auth_callers AS c ON c.id = p.caller_id "
            "AND c.library_id = p.home_library_id "
            "WHERE k.id IS NULL OR c.id IS NULL OR c.kind != 'agent'",
        )
        if invalid_library_policies:
            raise BackupDatabaseError

    rotations: dict[str, tuple[str, str, str | None, int | None, int | None, int]] = {}
    for (
        identifier,
        library_id,
        caller_id,
        target,
        rotated_at,
        revoked_at,
        created_at,
    ) in connection.execute(
        "SELECT id, library_id, caller_id, rotated_to_credential_id, rotated_at, "
        "revoked_at, created_at FROM auth_credentials"
    ):
        if not all(isinstance(value, str) for value in (identifier, library_id, caller_id)):
            raise BackupDatabaseError
        if target is not None and not isinstance(target, str):
            raise BackupDatabaseError
        if rotated_at is not None and type(rotated_at) is not int:
            raise BackupDatabaseError
        if revoked_at is not None and type(revoked_at) is not int:
            raise BackupDatabaseError
        if type(created_at) is not int:
            raise BackupDatabaseError
        rotations[identifier] = (
            library_id,
            caller_id,
            target,
            rotated_at,
            revoked_at,
            created_at,
        )

    targets: set[str] = set()
    for identifier, (
        library_id,
        caller_id,
        target,
        rotated_at,
        revoked_at,
        _created_at,
    ) in rotations.items():
        if target is None:
            continue
        if target in targets or rotated_at is None or revoked_at != rotated_at:
            raise BackupDatabaseError
        targets.add(target)
        target_row = rotations.get(target)
        if target_row is None or target_row[0:2] != (library_id, caller_id):
            raise BackupDatabaseError
        if target_row[5] != rotated_at:
            raise BackupDatabaseError
        seen = {identifier}
        cursor: str | None = target
        while cursor is not None:
            if cursor in seen:
                raise BackupDatabaseError
            seen.add(cursor)
            next_row = rotations.get(cursor)
            if next_row is None:
                raise BackupDatabaseError
            cursor = next_row[2]


def _require_lifecycle_replay(
    connection: sqlite3.Connection,
    *,
    library_id: str,
    caller_id: str,
    method: str,
    route: str,
    status: int,
    parsed: dict[str, object],
    location: str | None,
    etag: str,
    original_request_id: str,
    original_request_timestamp: str,
) -> None:
    action = "delete" if route == DELETE_PAGE_ROUTE_TEMPLATE else "restore"
    expected_method = "DELETE" if action == "delete" else "POST"
    if method != expected_method or status != 200:
        raise BackupDatabaseError
    try:
        body = PageLifecycleResponseBody.model_validate(parsed)
        operation_at = parse_occurrence_time(original_request_timestamp).utc_microseconds
    except ValueError:
        raise BackupDatabaseError from None
    matching = connection.execute(
        "SELECT p.page_uid, p.page_type, e.action, e.old_deleted_at, "
        "e.old_updated_at, e.changed_at, e.at_revision_number, "
        "e.occurred_at_at_event, e.actor_caller_id, r.revision_id "
        "FROM pages AS p JOIN page_lifecycle_events AS e "
        "ON e.library_id = p.library_id AND e.page_uid = p.page_uid "
        "JOIN revisions AS r ON r.library_id = p.library_id "
        "AND r.page_uid = p.page_uid AND r.revision_number = e.at_revision_number "
        "WHERE p.library_id = ? AND p.section_id = ? AND p.page_id = ? "
        "AND e.request_id = ? AND e.changed_at = ? LIMIT 2",
        (
            library_id,
            body.section_id,
            body.page_id,
            original_request_id,
            operation_at,
        ),
    ).fetchall()
    if len(matching) != 1:
        raise BackupDatabaseError
    (
        page_uid,
        page_type,
        event_action,
        old_deleted_at,
        old_updated_at,
        changed_at,
        at_revision,
        occurred_at,
        actor,
        revision_id,
    ) = matching[0]
    expected_page_location = f"/api/v1/sections/{body.section_id}/pages/{body.page_id}"
    expected_citation = f"{expected_page_location}/revisions/{at_revision}"
    if (
        type(page_uid) is not bytes
        or page_type != "archive"
        or event_action != action
        or (old_deleted_at is not None and type(old_deleted_at) is not int)
        or type(old_updated_at) is not int
        or type(changed_at) is not int
        or type(at_revision) is not int
        or type(occurred_at) is not int
        or actor != caller_id
        or type(revision_id) is not str
        or body.state != ("trashed" if action == "delete" else "active")
        or body.deleted_at != (canonical_utc_wire(changed_at) if action == "delete" else None)
        or body.updated_at != canonical_utc_wire(changed_at)
        or body.current_revision_id != revision_id
        or body.current_revision_number != at_revision
        or body.citation.href != expected_citation
        or location != expected_page_location
        or original_request_timestamp != canonical_utc_wire(changed_at)
        or etag != page_current_etag(page_uid, revision_id, at_revision, occurred_at, changed_at)
    ):
        raise BackupDatabaseError
    audit = connection.execute(
        "SELECT count(*) FROM auth_audit_events "
        "WHERE library_id = ? AND actor_caller_id = ? AND action = ? "
        "AND resource_type = 'page' AND resource_id = ? "
        "AND outcome = 'succeeded' AND request_id = ? AND occurred_at = ?",
        (
            library_id,
            caller_id,
            f"content.archive.{action}",
            body.page_id,
            original_request_id,
            changed_at,
        ),
    ).fetchone()
    if audit != (1,):
        raise BackupDatabaseError


class _FileSetReplayFile(ContentSchema):
    name: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _FileSetCreateReplayBody(ContentSchema):
    section_id: OpaqueId
    book_id: OpaqueId
    page_id: PageId
    revision_id: RevisionId
    revision_number: Literal[1]
    occurred_at: str
    occurrence_defaulted: bool
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: tuple[_FileSetReplayFile, ...]


class _FileSetAppendReplayBody(ContentSchema):
    changed: bool
    section_id: OpaqueId
    page_id: PageId
    revision_id: RevisionId
    revision_number: int = Field(ge=1)
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: tuple[_FileSetReplayFile, ...]


def _file_set_occurrence_at(
    connection: sqlite3.Connection,
    library_id: str,
    page_uid: bytes,
    current_occurrence: int,
    at: int,
) -> int:
    corrections = connection.execute(
        "SELECT old_occurred_at, new_occurred_at, corrected_at "
        "FROM page_occurrence_corrections WHERE library_id = ? AND page_uid = ? "
        "ORDER BY sequence",
        (library_id, page_uid),
    ).fetchall()
    occurrence = corrections[0][0] if corrections else current_occurrence
    if type(occurrence) is not int:
        raise BackupDatabaseError
    for _old, new, corrected_at in corrections:
        if type(new) is not int or type(corrected_at) is not int:
            raise BackupDatabaseError
        if corrected_at >= at:
            break
        occurrence = new
    return occurrence


def _file_set_valid_current_etags(
    connection: sqlite3.Connection,
    *,
    library_id: str,
    page_uid: bytes,
    revision_id: str,
    revision_number: int,
    revision_at: int,
    occurrence: int,
) -> set[str]:
    """Reconstruct active states of one Revision, including later corrections."""

    valid = {page_current_etag(page_uid, revision_id, revision_number, occurrence, revision_at)}
    events: list[tuple[int, str, int | str]] = []
    for new, changed_at in connection.execute(
        "SELECT new_occurred_at, corrected_at FROM page_occurrence_corrections "
        "WHERE library_id = ? AND page_uid = ? AND at_revision_number = ?",
        (library_id, page_uid, revision_number),
    ):
        if type(new) is not int or type(changed_at) is not int:
            raise BackupDatabaseError
        events.append((changed_at, "correction", new))
    for action, changed_at in connection.execute(
        "SELECT action, changed_at FROM page_lifecycle_events "
        "WHERE library_id = ? AND page_uid = ? AND at_revision_number = ?",
        (library_id, page_uid, revision_number),
    ):
        if action not in {"delete", "restore"} or type(changed_at) is not int:
            raise BackupDatabaseError
        events.append((changed_at, "lifecycle", action))
    active = True
    for changed_at, kind, value in sorted(events, key=lambda event: event[0]):
        if changed_at <= revision_at:
            raise BackupDatabaseError
        if kind == "correction":
            if type(value) is not int:
                raise BackupDatabaseError
            occurrence = value
        else:
            active = value == "restore"
        if active:
            valid.add(
                page_current_etag(page_uid, revision_id, revision_number, occurrence, changed_at)
            )
    return valid


def _require_file_set_replay(
    connection: sqlite3.Connection,
    *,
    library_id: str,
    caller_id: str,
    method: str,
    route: str,
    status: int,
    body_bytes: bytes,
    location: str | None,
    etag: str,
    original_request_id: str,
    original_request_timestamp: str,
) -> None:
    is_create = route == FILE_SET_CREATE_ROUTE_TEMPLATE
    if method != "POST" or status != (201 if is_create else 200) or location is not None:
        raise BackupDatabaseError
    try:
        operation_at = parse_occurrence_time(original_request_timestamp).utc_microseconds
        if original_request_timestamp != canonical_utc_wire(operation_at):
            raise ValueError("Noncanonical file-set operation timestamp.")
        body: _FileSetCreateReplayBody | _FileSetAppendReplayBody
        if is_create:
            body = _FileSetCreateReplayBody.model_validate_json(body_bytes)
        else:
            body = _FileSetAppendReplayBody.model_validate_json(body_bytes)
    except ValueError:
        raise BackupDatabaseError from None
    row = connection.execute(
        "SELECT p.page_uid, p.book_id, p.page_type, p.occurred_at, p.created_at, "
        "r.created_at, m.storage_format, m.snapshot_sha256 "
        "FROM pages AS p JOIN revisions AS r "
        "ON r.library_id = p.library_id AND r.page_uid = p.page_uid "
        "JOIN revision_file_sets AS m ON m.library_id = r.library_id "
        "AND m.page_uid = r.page_uid AND m.revision_id = r.revision_id "
        "AND m.revision_number = r.revision_number "
        "WHERE p.library_id = ? AND p.section_id = ? AND p.page_id = ? "
        "AND r.revision_id = ? AND r.revision_number = ?",
        (library_id, body.section_id, body.page_id, body.revision_id, body.revision_number),
    ).fetchone()
    if row is None:
        raise BackupDatabaseError
    page_uid, book_id, page_type, current_occurrence, created_at, revision_at, format_, snapshot = (
        row
    )
    if (
        type(page_uid) is not bytes
        or type(book_id) is not str
        or page_type != "archive"
        or type(current_occurrence) is not int
        or type(created_at) is not int
        or type(revision_at) is not int
        or format_ not in {"legacy_markdown", "file_set_v1"}
    ):
        raise BackupDatabaseError
    actual_files = connection.execute(
        "SELECT filename, content_bytes, size_bytes, content_sha256 FROM revision_files "
        "WHERE library_id = ? AND page_uid = ? AND revision_id = ? AND revision_number = ? "
        "ORDER BY filename",
        (library_id, page_uid, body.revision_id, body.revision_number),
    ).fetchall()
    try:
        manifest = build_file_manifest(
            (name, content) for name, content, _size, _hash in actual_files
        )
    except (TypeError, ValueError, OverflowError, UnicodeError):
        raise BackupDatabaseError from None
    if (
        manifest.snapshot_sha256.hex() != body.snapshot_sha256
        or (format_ == "file_set_v1" and snapshot != manifest.snapshot_sha256)
        or (format_ == "legacy_markdown" and snapshot is not None)
    ):
        raise BackupDatabaseError
    response_files = [(entry.name, entry.size_bytes, entry.sha256) for entry in body.files]
    if response_files != [
        (name, size, digest.hex() if type(digest) is bytes else None)
        for name, _content, size, digest in actual_files
    ]:
        raise BackupDatabaseError

    if is_create:
        if not isinstance(body, _FileSetCreateReplayBody):
            raise BackupDatabaseError
        occurrence = _file_set_occurrence_at(
            connection, library_id, page_uid, current_occurrence, revision_at
        )
        if (
            format_ != "file_set_v1"
            or body.book_id != book_id
            or revision_at != created_at
            or revision_at != operation_at
            or body.occurred_at != canonical_utc_wire(occurrence)
            or (body.occurrence_defaulted and occurrence != operation_at)
            or etag != page_current_etag(page_uid, body.revision_id, 1, occurrence, revision_at)
        ):
            raise BackupDatabaseError
        action = "content.page.file_set.create"
        resource_type = "page"
        resource_id = body.page_id
        changed = True
    else:
        if not isinstance(body, _FileSetAppendReplayBody):
            raise BackupDatabaseError
        if body.changed:
            occurrence = _file_set_occurrence_at(
                connection, library_id, page_uid, current_occurrence, revision_at
            )
            if (
                format_ != "file_set_v1"
                or revision_at < operation_at
                or etag
                != page_current_etag(
                    page_uid,
                    body.revision_id,
                    body.revision_number,
                    occurrence,
                    revision_at,
                )
            ):
                raise BackupDatabaseError
        else:
            occurrence = _file_set_occurrence_at(
                connection, library_id, page_uid, current_occurrence, revision_at
            )
            if etag not in _file_set_valid_current_etags(
                connection,
                library_id=library_id,
                page_uid=page_uid,
                revision_id=body.revision_id,
                revision_number=body.revision_number,
                revision_at=revision_at,
                occurrence=occurrence,
            ):
                raise BackupDatabaseError
        action = "content.page.file_set.revise"
        resource_type = "revision"
        resource_id = body.revision_id
        changed = body.changed
    if changed:
        source = connection.execute(
            "SELECT count(*) FROM page_sources WHERE library_id = ? AND page_uid = ? "
            "AND revision_id = ? AND revision_number = ? AND created_at = ?",
            (library_id, page_uid, body.revision_id, body.revision_number, operation_at),
        ).fetchone()
        if source != (1,):
            raise BackupDatabaseError
    audit = connection.execute(
        "SELECT count(*) FROM auth_audit_events WHERE library_id = ? "
        "AND actor_caller_id = ? AND action = ? AND resource_type = ? "
        "AND resource_id = ? AND outcome = 'succeeded' AND request_id = ? "
        "AND occurred_at = ?",
        (
            library_id,
            caller_id,
            action,
            resource_type,
            resource_id,
            original_request_id,
            operation_at,
        ),
    ).fetchone()
    # Request IDs are generated at the HTTP edge but are not a unique database
    # key. A no-op may share one with an earlier changed operation; do not
    # mistake that earlier audit event for a mutation made by this no-op.
    if changed and audit != (1,):
        raise BackupDatabaseError


def _require_idempotency_graph(connection: sqlite3.Connection, schema_revision: str) -> None:
    def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    for (
        library_id,
        caller_id,
        method,
        route,
        status,
        media_type,
        body_bytes,
        location,
        etag,
        original_request_id,
        original_request_timestamp,
    ) in connection.execute(
        "SELECT library_id, caller_id, method, route_template, response_status, "
        "response_media_type, response_body, response_location, response_etag, "
        "original_request_id, original_request_timestamp "
        "FROM idempotency_records"
    ):
        if (
            not isinstance(library_id, str)
            or not isinstance(caller_id, str)
            or method not in {"POST", "PATCH", "DELETE"}
            or not isinstance(route, str)
            or status not in {200, 201}
            or media_type != "application/json"
            or type(body_bytes) is not bytes
            or (location is not None and not isinstance(location, str))
            or not isinstance(etag, str)
            or not isinstance(original_request_id, str)
            or not isinstance(original_request_timestamp, str)
        ):
            raise BackupDatabaseError
        try:
            decoded = body_bytes.decode("utf-8", errors="strict")
            parsed = json.loads(
                decoded,
                object_pairs_hook=reject_duplicate_pairs,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
            if not isinstance(parsed, dict):
                raise ValueError
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise BackupDatabaseError from None

        if method == "PATCH":
            if (
                schema_revision not in _OCCURRENCE_REVISIONS
                or route != CORRECT_OCCURRENCE_ROUTE_TEMPLATE
                or status != 200
            ):
                raise BackupDatabaseError
            try:
                correction_body = OccurrenceCorrectionResponseBody.model_validate(parsed)
                correction_time = parse_occurrence_time(original_request_timestamp)
            except ValueError:
                raise BackupDatabaseError from None
            matching = connection.execute(
                "SELECT p.page_uid, p.page_type, c.old_occurred_at, "
                "c.new_occurred_at, c.at_revision_number, c.actor_caller_id, "
                "c.corrected_at, r.revision_id FROM pages AS p "
                "JOIN page_occurrence_corrections AS c ON c.library_id = p.library_id "
                "AND c.page_uid = p.page_uid "
                "JOIN revisions AS r ON r.library_id = p.library_id "
                "AND r.page_uid = p.page_uid AND r.revision_number = c.at_revision_number "
                "WHERE p.library_id = ? AND p.section_id = ? AND p.page_id = ? "
                "AND c.corrected_at = ? LIMIT 2",
                (
                    library_id,
                    correction_body.section_id,
                    correction_body.page_id,
                    correction_time.utc_microseconds,
                ),
            ).fetchall()
            if len(matching) != 1:
                raise BackupDatabaseError
            (
                page_uid,
                page_type,
                old_occurrence,
                new_occurrence,
                at_revision,
                actor,
                corrected_at,
                revision_id,
            ) = matching[0]
            expected_citation = (
                f"/api/v1/sections/{correction_body.section_id}/pages/"
                f"{correction_body.page_id}/revisions/{at_revision}"
            )
            expected_location = (
                f"/api/v1/sections/{correction_body.section_id}/pages/{correction_body.page_id}"
            )
            if (
                type(page_uid) is not bytes
                or page_type != "archive"
                or type(old_occurrence) is not int
                or type(new_occurrence) is not int
                or type(at_revision) is not int
                or actor != caller_id
                or type(corrected_at) is not int
                or not isinstance(revision_id, str)
                or correction_body.previous_occurred_at != canonical_utc_wire(old_occurrence)
                or correction_body.occurred_at != canonical_utc_wire(new_occurrence)
                or correction_body.current_revision_id != revision_id
                or correction_body.current_revision_number != at_revision
                or correction_body.citation.href != expected_citation
                or location != expected_location
                or original_request_timestamp != canonical_utc_wire(corrected_at)
                or etag
                != page_current_etag(
                    page_uid, revision_id, at_revision, new_occurrence, corrected_at
                )
            ):
                raise BackupDatabaseError
            audit = connection.execute(
                "SELECT count(*) FROM auth_audit_events "
                "WHERE library_id = ? AND actor_caller_id = ? "
                "AND action = 'content.archive.correct_occurrence' "
                "AND resource_type = 'page' AND resource_id = ? "
                "AND outcome = 'succeeded' AND request_id = ? AND occurred_at = ?",
                (
                    library_id,
                    caller_id,
                    correction_body.page_id,
                    original_request_id,
                    corrected_at,
                ),
            ).fetchone()
            if audit is None or audit[0] != 1:
                raise BackupDatabaseError
            continue

        if route in {DELETE_PAGE_ROUTE_TEMPLATE, RESTORE_PAGE_ROUTE_TEMPLATE}:
            if schema_revision not in _LIFECYCLE_REVISIONS:
                raise BackupDatabaseError
            _require_lifecycle_replay(
                connection,
                library_id=library_id,
                caller_id=caller_id,
                method=method,
                route=route,
                status=status,
                parsed=parsed,
                location=location,
                etag=etag,
                original_request_id=original_request_id,
                original_request_timestamp=original_request_timestamp,
            )
            continue

        if route in {FILE_SET_CREATE_ROUTE_TEMPLATE, FILE_SET_APPEND_ROUTE_TEMPLATE}:
            if schema_revision not in _FILE_SET_REVISIONS:
                raise BackupDatabaseError
            _require_file_set_replay(
                connection,
                library_id=library_id,
                caller_id=caller_id,
                method=method,
                route=route,
                status=status,
                body_bytes=body_bytes,
                location=location,
                etag=etag,
                original_request_id=original_request_id,
                original_request_timestamp=original_request_timestamp,
            )
            continue

        if status != 201 or route not in {CREATE_ROUTE_TEMPLATE, REVISE_ROUTE_TEMPLATE}:
            raise BackupDatabaseError
        try:
            body = ArchiveResponseBody.model_validate(parsed)
        except ValueError:
            raise BackupDatabaseError from None

        row = connection.execute(
            "SELECT p.page_uid, p.book_id, p.title, p.page_type, p.occurred_at, "
            "r.created_at, r.content_md, r.content_sha256, p.created_at FROM pages AS p "
            "JOIN revisions AS r ON r.library_id = p.library_id AND r.page_uid = p.page_uid "
            "AND r.revision_id = ? AND r.revision_number = ? "
            "WHERE p.library_id = ? AND p.page_id = ? AND p.section_id = ?",
            (
                body.revision.revision_id,
                body.revision.revision_number,
                library_id,
                body.page.page_id,
                body.page.section_id,
            ),
        ).fetchone()
        if (
            row is None
            or type(row[0]) is not bytes
            or not isinstance(row[1], str)
            or not isinstance(row[2], str)
            or not isinstance(row[3], str)
            or type(row[4]) is not int
            or type(row[5]) is not int
            or type(row[6]) is not bytes
            or type(row[7]) is not bytes
            or type(row[8]) is not int
        ):
            raise BackupDatabaseError
        response_occurrence = row[4]
        if schema_revision in _OCCURRENCE_REVISIONS:
            corrections = connection.execute(
                "SELECT old_occurred_at, new_occurred_at, at_revision_number "
                "FROM page_occurrence_corrections WHERE library_id = ? AND page_uid = ? "
                "ORDER BY sequence",
                (library_id, row[0]),
            ).fetchall()
            if corrections:
                response_occurrence = corrections[0][0]
                for _old, new, at_revision in corrections:
                    if at_revision < body.revision.revision_number:
                        response_occurrence = new
                    else:
                        break
        if (
            body.page.book_id != row[1]
            or body.page.title != row[2]
            or body.page.type != row[3]
            or body.page.occurred_at != canonical_utc_wire(response_occurrence)
            or body.revision.created_at != canonical_utc_wire(row[5])
        ):
            raise BackupDatabaseError
        if body.occurrence_notice is not None and (
            route != CREATE_ROUTE_TEMPLATE
            or body.revision.revision_number != 1
            or row[8] != row[5]
            or response_occurrence != row[5]
            or body.page.occurred_at != original_request_timestamp
            or etag.startswith('"page-v1-')
        ):
            raise BackupDatabaseError
        try:
            stored_content = row[6].decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise BackupDatabaseError from None
        if stored_content != body.revision.content or row[7].hex() != body.revision.content_sha256:
            raise BackupDatabaseError
        if etag.startswith('"page-v1-'):
            expected_etag = legacy_page_current_etag(
                row[0], body.revision.revision_id, body.revision.revision_number
            )
        else:
            # Archive writes stamp the new Revision and Page with the same
            # monotonic logical time. Its immutable Revision row therefore
            # reconstructs the original v2 header after later corrections.
            expected_etag = page_current_etag(
                row[0],
                body.revision.revision_id,
                body.revision.revision_number,
                response_occurrence,
                row[5],
            )
        if expected_etag != etag:
            raise BackupDatabaseError
        page_location = f"/api/v1/sections/{body.page.section_id}/pages/{body.page.page_id}"
        revision_location = f"{page_location}/revisions/{body.revision.revision_number}"
        if body.citation.href != revision_location:
            raise BackupDatabaseError
        expected_location = page_location if route == CREATE_ROUTE_TEMPLATE else revision_location
        if route not in {CREATE_ROUTE_TEMPLATE, REVISE_ROUTE_TEMPLATE}:
            raise BackupDatabaseError
        if location != expected_location:
            raise BackupDatabaseError


def _require_tag_graph(connection: sqlite3.Connection) -> None:
    """Reject Tag rows whose Unicode matching key disagrees with their display name."""

    for display_name, match_key, created_at in connection.execute(
        "SELECT display_name, match_key, created_at FROM tags"
    ):
        if (
            type(display_name) is not str
            or type(match_key) is not str
            or type(created_at) is not int
            or created_at < 0
        ):
            raise BackupDatabaseError
        try:
            canonical_display, canonical_key = normalize_tag_name(display_name)
        except ValueError:
            raise BackupDatabaseError from None
        if display_name != canonical_display or match_key != canonical_key:
            raise BackupDatabaseError
    for (created_at,) in connection.execute("SELECT created_at FROM page_tags"):
        if type(created_at) is not int or created_at < 0:
            raise BackupDatabaseError


def _validate_connection(
    connection: sqlite3.Connection, schema_revision: str
) -> DatabaseValidationReport:
    connection.execute("PRAGMA query_only = ON")
    _require_sqlite_integrity(connection)
    schema_revision = _require_schema(connection, schema_revision)
    sqlite_version_row = connection.execute("SELECT sqlite_version()").fetchone()
    journal_row = connection.execute("PRAGMA journal_mode").fetchone()
    if (
        sqlite_version_row is None
        or not isinstance(sqlite_version_row[0], str)
        or journal_row is None
        or not isinstance(journal_row[0], str)
    ):
        raise BackupDatabaseError
    _require_page_graph(connection, schema_revision)
    if schema_revision in _LIFECYCLE_REVISIONS:
        _require_lifecycle_graph(connection)
    if schema_revision not in {
        LEGACY_SCHEMA_REVISION,
        PREVIOUS_SCHEMA_REVISION,
        INTERMEDIATE_SCHEMA_REVISION,
    }:
        _require_tag_graph(connection)
    _require_auth_graph(connection, schema_revision)
    _require_idempotency_graph(connection, schema_revision)
    return DatabaseValidationReport(
        schema_revision=schema_revision,
        sqlite_version=sqlite_version_row[0],
        artifact_journal_mode=journal_row[0].lower(),
    )


def validate_database(
    path: Path, *, schema_revision: str = SUPPORTED_SCHEMA_REVISION
) -> DatabaseValidationReport:
    """Validate one closed, self-contained SQLite file without modifying it."""

    if not isinstance(path, Path) or not path.is_absolute():
        raise BackupDatabaseError
    try:
        if path.is_symlink() or not path.is_file():
            raise BackupDatabaseError
        with closing(sqlite3.connect(_read_only_uri(path), uri=True, timeout=5.0)) as connection:
            report = _validate_connection(connection, schema_revision)
    except BackupDatabaseError:
        raise
    except (OSError, RecursionError, sqlite3.Error, ValueError):
        raise BackupDatabaseError from None
    if report.artifact_journal_mode != "delete":
        raise BackupDatabaseError
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(f"{path}{suffix}").exists():
            raise BackupDatabaseError
    return report


__all__ = ["DatabaseValidationReport", "validate_database"]

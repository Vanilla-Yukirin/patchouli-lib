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

from patchouli_lib.admin.file_set_receipt_validation import (
    MasterFileSetReceiptCorruptError,
    validate_master_file_set_receipt,
)
from patchouli_lib.admin.file_set_receipts import MasterFileSetReceipt
from patchouli_lib.admin.move_receipts import (
    MasterMoveReceipt,
    MasterMoveReceiptCorruptError,
    validate_master_move_receipt,
)
from patchouli_lib.admin.passwords import parse_password_hash
from patchouli_lib.auth.tokens import InvalidTokenError, parse_token, verify_token
from patchouli_lib.backup.errors import BackupDatabaseError
from patchouli_lib.backup.manifest import (
    ACTOR_HOME_SCHEMA_REVISION,
    AGENT_TOKEN_VALUES_SCHEMA_REVISION,
    AUDIT_ACTOR_INDEX_SCHEMA_REVISION,
    CALLER_PAGE_MOVE_SCHEMA_REVISION,
    FILE_SET_SCHEMA_REVISION,
    INTERMEDIATE_SCHEMA_REVISION,
    LEGACY_SCHEMA_REVISION,
    LIBRARY_DESCRIPTION_SCHEMA_REVISION,
    LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    LIBRARY_POLICY_SCHEMA_REVISION,
    LIFECYCLE_SCHEMA_REVISION,
    MASTER_AUDIT_SCHEMA_REVISION,
    MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION,
    MASTER_IDENTITY_SCHEMA_REVISION,
    MASTER_LIFECYCLE_SCHEMA_REVISION,
    MASTER_OCCURRENCE_SCHEMA_REVISION,
    MASTER_PAGE_DELETE_SCHEMA_REVISION,
    OCCURRENCE_SCHEMA_REVISION,
    PAGE_MOVE_SCHEMA_REVISION,
    PAGE_TITLE_SCHEMA_REVISION,
    PREVIOUS_SCHEMA_REVISION,
    REQUEST_LOG_SCHEMA_REVISION,
    SEARCH_INDEX_SCHEMA_REVISION,
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
from patchouli_lib.content.page_lifecycle_receipts import require_library_lifecycle_graph
from patchouli_lib.content.page_lifecycle_schemas import (
    LIBRARY_LIFECYCLE_ROUTES,
    LibraryPageLifecycleBody,
    lifecycle_route,
)
from patchouli_lib.content.page_membership_history import (
    PageMembershipHistoryError,
    PageStateTimeline,
    load_page_state_timeline,
)
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
from patchouli_lib.request_log.repository import RequestLogWrite
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
_EXPECTED_SQL_HASHES_0015: Final = _EXPECTED_SQL_HASHES_0014 | {
    # Generated from an empty Alembic 0015 database with _canonical_schema_sql.
    ("table", "auth_agent_token_values"): (
        "9613b1ce738ebad3069cc68ddfaed201b0335620d61dfd9ee4a9fa33c2b6c9ae"
    ),
    ("trigger", "trg_auth_agent_token_values_agent_only"): (
        "93d58518f6842ce38d8b1e96ad08de08899df7a12df7139f21820f8aae07a8b0"
    ),
    ("trigger", "trg_auth_agent_token_values_immutable"): (
        "35b1f05c838bd960d6eebb54726383ad75ea60d6408808734cae6723208d0432"
    ),
    ("trigger", "trg_auth_agent_token_values_revoke"): (
        "6690c0078b2dc95a9ef5827e2dc99a4749fd335598b46d83dcd02db70ffab9de"
    ),
    ("trigger", "trg_auth_agent_token_values_disable"): (
        "c65d2ee5c3cdba8d1d59c3a0900ab3cdb7ce4b256de1c61e2e97ac2a3e996313"
    ),
}
_EXPECTED_SQL_HASHES_0016: Final = _EXPECTED_SQL_HASHES_0015 | {
    # Generated from an empty Alembic 0016 database with _canonical_schema_sql.
    ("table", "admin_master_identity"): (
        "b71ede2b5ef3092872e5e652ca1e0995ae05d8d9770c134d97c6874e053ae50f"
    ),
}
_EXPECTED_SQL_HASHES_0017: Final = _EXPECTED_SQL_HASHES_0016 | {
    # Generated from an empty Alembic 0017 database with _canonical_schema_sql.
    ("table", "auth_audit_events"): (
        "d8f627b4fe2abd9b943032cd343d5dbe8e241d3b6f9e33770ae417683d15fac1"
    ),
    ("table", "idempotency_records"): (
        "29ee0c3087f9a7245a18ec37ac61c8d58d814a572d11a065629c7563a34f1bd6"
    ),
    ("table", "page_lifecycle_events"): (
        "e28206284603352ab3049dd2ee185f6a3d197a28f66b5b28b964e2635b1c1bdb"
    ),
    ("table", "page_lifecycle_guards"): (
        "975cdf9fbafb9a93b2fb1039375ebdbf6395223be615bd0bb357011b9f990317"
    ),
    ("table", "page_occurrence_correction_guards"): (
        "b4927d6b7d02a7e85fcfbdb5894349d298b8361510554eaaf57d8dec69b4bb00"
    ),
    ("table", "page_occurrence_corrections"): (
        "71dbfb918b79aa5c02281cb43e2a5e066f48aeaf78ea6066e804c6635e4ebe93"
    ),
    ("trigger", "trg_page_lifecycle_events_validate_insert"): (
        "5f5a4b9fcb94f63b6d1a26575b9ff716ef72a0a03ee688431331662b65e68f28"
    ),
    ("trigger", "trg_page_lifecycle_guards_safe_delete"): (
        "72f86075147169aeeaafa2d5546f314daa964a0e3536ed599f672f69a7ed70b9"
    ),
    ("trigger", "trg_page_occurrence_corrections_validate_insert"): (
        "a1fad9cab97d36483f667252940055906462bce69f8e378932aa013fd261031f"
    ),
    ("trigger", "trg_page_occurrence_guards_safe_delete"): (
        "06a8344ab825176a6a4bd1be17b210861a5ab3a97ca406f3660243d32eb977f1"
    ),
    ("trigger", "trg_pages_lifecycle_record"): (
        "ff98fe1910ed7d5b1522aabd8478e644e929358516fc14cdc382ca1507288178"
    ),
    ("trigger", "trg_pages_occurrence_record"): (
        "962cda0022818ec6e82522445303e067c0441d4d9ed690177f275ca108cbe3eb"
    ),
}
_EXPECTED_SQL_HASHES_0018: Final = _EXPECTED_SQL_HASHES_0017 | {
    # Generated from an empty Alembic 0018 database with _canonical_schema_sql.
    ("table", "admin_master_audit_events"): (
        "62d21703e6a71e85a2c3791a40767d2d3cc0fd2d766f076487b0516eea1405f9"
    ),
    ("trigger", "trg_admin_master_audit_no_update"): (
        "f057a928459fb39c4b2456c2b048cb6d1e685a3b47026375c007838e49650b9d"
    ),
    ("trigger", "trg_admin_master_audit_no_delete"): (
        "287d1c6b1766e5f682fdc182052dd24fb6755b1deff8b36621c6ccb62857acbc"
    ),
}
_EXPECTED_SQL_HASHES_0019: Final = _EXPECTED_SQL_HASHES_0018 | {
    # Generated from an empty Alembic 0019 database with _canonical_schema_sql.
    ("table", "page_lifecycle_events"): (
        "3fd8f2a2d0d52e8447d9d5761b7ee8a7de8bde0f1edbb839ca392152230882d7"
    ),
    ("table", "page_lifecycle_guards"): (
        "3337688aa38ce231e930b6dd06206d5d2f01f9a400451076369b3d3c0a6f0450"
    ),
    ("trigger", "trg_page_lifecycle_guards_safe_delete"): (
        "41d71e7471b5a74fc63418e78298715b7aff263d6d34a4ac592283b8af6720b6"
    ),
    ("trigger", "trg_page_lifecycle_events_validate_insert"): (
        "00c561140529fea90464c204ddaa9dad496e2c7939c52ca99c6db7e1735e08f3"
    ),
    ("trigger", "trg_pages_lifecycle_record"): (
        "7a3f8d607a77032e4df947ffe392b9370319e97f27dd9632cb82e9b3465a6b14"
    ),
    ("trigger", "trg_page_lifecycle_guards_master_audit"): (
        "4670af1144747e77b01dbe6eb6087a9906d00693a7bae577786ebc04c4c5809d"
    ),
}
_EXPECTED_SQL_HASHES_0020: Final = _EXPECTED_SQL_HASHES_0019 | {
    # Derived from a fresh Alembic 0020 database with _canonical_schema_sql.
    ("table", "libraries"): ("66347c3beb95842c572615086d35e0b22bc4bdd410d4a01d967bc7e426dc615d"),
}
_EXPECTED_SQL_HASHES_0021: Final = _EXPECTED_SQL_HASHES_0020 | {
    # Derived from a fresh Alembic 0021 database with _canonical_schema_sql.
    ("index", "ix_auth_audit_events_actor_recent"): (
        "2be933d76691f5d3cb03d618725a052dc0a87f34c5110cd6674a2c69bb0ee73d"
    ),
}
_EXPECTED_SQL_HASHES_0022: Final = _EXPECTED_SQL_HASHES_0021 | {
    # Derived from a fresh Alembic 0022 database with _canonical_schema_sql.
    ("table", "page_title_events"): (
        "116d847b506abb91312a34213e32165955b63417c80fd3892f205ab285f4738d"
    ),
    ("trigger", "trg_pages_title_require_audit"): (
        "e0714634ed0e909b8194703c1858cc2cc30f52fe0989e5f8751bcbd1fc74e9bf"
    ),
    ("trigger", "trg_page_title_events_validate_insert"): (
        "54445aa108b342c034491d63cd0824486db7722eaf1b2f4746ba35af12162523"
    ),
    ("trigger", "trg_pages_title_record"): (
        "07231402ac1a697f27c26ca7251c8af6f01a2bdb4bbfca30a212e4b6d2ccb0ef"
    ),
    ("trigger", "trg_page_title_events_no_update"): (
        "d495a523de839773464634b97742449c063ddcc2141c80a0643eceda9e46c07e"
    ),
    ("trigger", "trg_page_title_events_no_delete"): (
        "cecfbe938566ce8d1a802c0229786483aad5f7ad120319869783ef033ce5759f"
    ),
}
_EXPECTED_SQL_HASHES_0023: Final = _EXPECTED_SQL_HASHES_0022 | {
    # Derived from a fresh Alembic 0023 database with _canonical_schema_sql.
    ("table", "api_request_log"): (
        "922848bfed6e46c2030da4873f82ed4e9a3f8045e93eee9e96aee7ee25014e4c"
    ),
    ("index", "ix_api_request_log_retention"): (
        "627bb5c0e0c1e3809e28726954e4de105a04a007f0374dda6d2a7202960701fd"
    ),
    ("index", "ix_api_request_log_actor_recent"): (
        "f9d817faaf404a7183b30b6ff702b0f85400b36e6a3829e4606487933099645c"
    ),
}
_EXPECTED_SQL_HASHES_0024: Final = _EXPECTED_SQL_HASHES_0023 | {
    # Derived from a fresh Alembic 0024 database with _canonical_schema_sql.
    # The FTS5 virtual table and its SQLite-managed shadow tables are all covered.
    (
        "index",
        "ix_search_dirty_pages_seq",
    ): "15966f8f6ccc2da7566fd59537d05ae6a3d85f343f4835a95e3a49a0a074d5a6",
    (
        "index",
        "ix_search_documents_page",
    ): "4ca8f2af69a224d6b2c0be65ee1846985401409e4a2affdfb5a79af55882216b",
    (
        "index",
        "ix_search_page_state_scope_time",
    ): "7f95b156dfc79a300f064452ddbc5ae4bc49e5f20dd1f5d23bc225be62e289e0",
    (
        "table",
        "search_dirty_pages",
    ): "e176c290ce5cfecaec5f95bb8966a1ec857af68e34f3388f54ff4d7ad387eb89",
    (
        "table",
        "search_documents",
    ): "e65fd91688c975993252db7838dae914b4db75cdd92aba9278fa7bba134c0f7f",
    (
        "table",
        "search_generations",
    ): "3a402a674f989e64d67354c1a9f2fb6897d9c6cd7b9af9566b2630bdc742b118",
    ("table", "search_meta"): "91f0f113e3edfcb21fcac3e4b6089ee7ebbaf00e2258dc904a59044b82902cf5",
    (
        "table",
        "search_page_state",
    ): "bfb3458e5047ad5fcee45935212cfc7b516e64eb0a0041a5a07fdfe1a167cbb1",
    ("table", "search_terms"): "2da48a6e618833323abfc98cedd2e1f5063bcaeeba19ac084b30279517aa8322",
    (
        "table",
        "search_terms_config",
    ): "ac81c9eecdf7490e9839aee2fd11adf44087dfbe152eb2433bce8c4de43a59d4",
    (
        "table",
        "search_terms_content",
    ): "3bc8a6703cd17e356a92dfbad39c8e1164ec97a77da881247dfd284853208c2d",
    (
        "table",
        "search_terms_data",
    ): "7e3fda5217d2c18242c47b7331cf9c471efdceac34323c75d54ae2b5242c0be5",
    (
        "table",
        "search_terms_docsize",
    ): "e6b7e3509cbdd97d8946fb2b5dff64c580ce266dd3f1e462779ffa60ff801b2b",
    (
        "table",
        "search_terms_idx",
    ): "14dc41b2569e9ba4796256ba024d65ba68446042fbe2a1a6c5bbf6e589268a2f",
    (
        "trigger",
        "trg_search_page_tags_delete",
    ): "3e2a033c722308ab703d8692ef9bd30fcf5182d986ab17edbc84d7be38b8dd71",
    (
        "trigger",
        "trg_search_page_tags_insert",
    ): "2a13b5f4dccce1668a0c6c83d0bd5ff563be527cf36d884552901d69e09b65ac",
    (
        "trigger",
        "trg_search_page_tags_update",
    ): "79b8fff6b51fe40e927a9b077a9dc968c68168e458e6ea0ba591667f611c813e",
    (
        "trigger",
        "trg_search_page_tags_update_old",
    ): "870de7c4d3dd76c1434d9b6ad9c3406e31d94cf9246a74a2f6c51055be2fe637",
    (
        "trigger",
        "trg_search_pages_delete",
    ): "5ce715b0db2686564e3c5ef4893c97b49c727acdfae05048443279881377dd9f",
    (
        "trigger",
        "trg_search_pages_insert",
    ): "2165f882a0b21d73ec2f02281eb4686b2c5c46ab4b186b931810a2d3e6608815",
    (
        "trigger",
        "trg_search_pages_update_new",
    ): "17b1da77103015a6a9d63717898e33e08cb6d9d5935496251a2683d8e0d7aa94",
    (
        "trigger",
        "trg_search_pages_update_old",
    ): "a511665c24f240e2dceb6fb2ced61d8cfbd119c17348eb2506c7ab44525722a5",
    (
        "trigger",
        "trg_search_revision_file_seals_delete",
    ): "773f9deab99bc49c55e60f3f3f7810d93612f2660fb6d13a448579b6c51e28bc",
    (
        "trigger",
        "trg_search_revision_file_seals_insert",
    ): "e7823aa9e8ac9b76dbd5b13efdd2eabc1c758621dfb256f26595d8155c6b60b5",
    (
        "trigger",
        "trg_search_revision_file_seals_update",
    ): "fc0de58a4b50bcdfd980f1159b0d33d5b302408798d21b472415bee8ccdd5bc6",
    (
        "trigger",
        "trg_search_revision_file_seals_update_old",
    ): "b943e139b1e3b67e02acb57b3c9fd8632252c0408e760e755605323fec04451b",
    (
        "trigger",
        "trg_search_revision_file_sets_delete",
    ): "99e7d1b52816bb2751eb9f2b95d64ef3a984831a4b23e50842d646c997b66e8b",
    (
        "trigger",
        "trg_search_revision_file_sets_insert",
    ): "a4e0fe9aa24724583b32172253cd155e90de70cb4de49c054a6b5a5a1ba8a62d",
    (
        "trigger",
        "trg_search_revision_file_sets_update",
    ): "3e2c797b50143f188462e8d155e9fca6bca797c854ba9d3bf5ea3dfc87bd1313",
    (
        "trigger",
        "trg_search_revision_file_sets_update_old",
    ): "747d07aa3c4b3cb1f0a13430918adc9f89e597125d19f2769e848a6f11d0745d",
    (
        "trigger",
        "trg_search_revision_files_delete",
    ): "46ce2caa7cdc032c2ed3253e9594270fc29bfd4d048ac1d5e434cdcccbcdfbfd",
    (
        "trigger",
        "trg_search_revision_files_insert",
    ): "548e7ce057c7a7ff8a2548c83313cbefca03be0488b3e4c061cc92aefa08270d",
    (
        "trigger",
        "trg_search_revision_files_update",
    ): "a9f6a8d97ef9d6f1a2d483c2e31204c8ef798f57853b73c38a7cae528cfa94d9",
    (
        "trigger",
        "trg_search_revision_files_update_old",
    ): "582e5ae259f0221464ce52910cc8addcb0a7ff7e659f66604a86d6615ed7646a",
    (
        "trigger",
        "trg_search_revisions_delete",
    ): "34f7e06e518d1a16d0878f4a08ee010a09eba87500c3e79a5fa8166c6c6e60cd",
    (
        "trigger",
        "trg_search_revisions_insert",
    ): "76145ff6dd4090991e2cac083bafa0dc1a79c3ca5aa55dff2f5ed6366d58ea54",
    (
        "trigger",
        "trg_search_revisions_update",
    ): "1312aeebb8bd31849669d21bc5f10a490dd8e3e7f6fffb63611625fc0b86fdf0",
    (
        "trigger",
        "trg_search_revisions_update_old",
    ): "914ba93c68c2e702100b997de2be3be32f900ff605334960e8bd737e46d29187",
}
_EXPECTED_SQL_HASHES_0025: Final = _EXPECTED_SQL_HASHES_0024 | {
    # Derived from a fresh Alembic 0025 database with _canonical_schema_sql.
    ("trigger", "trg_page_lifecycle_guards_master_audit"): (
        "b1ff6e8a67f7c3eda27b1454368ac2312ee54d1b00f2d2fce0eb5bf64c1e2924"
    ),
}
_EXPECTED_SQL_HASHES_0026: Final = _EXPECTED_SQL_HASHES_0025 | {
    # Derived from a fresh Alembic 0026 database with _canonical_schema_sql.
    ("table", "admin_master_file_set_receipts"): (
        "91597ee868abf68e4e636cc4a6b8656e0d7b8d06c52d08f9b8e879b4f8651244"
    ),
    ("trigger", "trg_master_file_set_receipts_no_update"): (
        "e9fdf74fb604a4a0ab75a3eabf1173f7ce11757295a289f0986ad5a9145b6aa6"
    ),
    ("trigger", "trg_master_file_set_receipts_no_delete"): (
        "be95a935ab564a48f9af4481a8a5ae184c76a4b9df261a7ebc32b2ca168c93b9"
    ),
}
_EXPECTED_SQL_HASHES_0027: Final = _EXPECTED_SQL_HASHES_0026 | {
    # Derived from a fresh Alembic 0027 database with _canonical_schema_sql.
    ("table", "page_occurrence_corrections"): (
        "67381beefbe599f4d56e6f8257da2a1ad62c31e98c667e8154683629298357cc"
    ),
    ("table", "page_occurrence_correction_guards"): (
        "5b37f14b2e0dc8b36fa71490d541fd259da3233ea510f91ac10dd82b6239d5cc"
    ),
    ("trigger", "trg_page_occurrence_guards_safe_delete"): (
        "7152532f4cd50643947ee9d3d04d1525573e0e7678d38b98722c21e6cd271821"
    ),
    ("trigger", "trg_page_occurrence_corrections_validate_insert"): (
        "001091ec10d11966e1e2c23e293b8a43e5ce591008bdf8494bf12c21d6a44c70"
    ),
    ("trigger", "trg_pages_occurrence_record"): (
        "22d9359887bcbb710f1e429d0c87d470b437f28883fd37bea4e894b1557f6733"
    ),
    ("trigger", "trg_page_occurrence_guards_master_audit"): (
        "0e3913f9d6b79ded7b8064b2d0fa8efaa8f9fd2d4987740c9b58be76edfc7034"
    ),
}
_EXPECTED_SQL_HASHES_0028: Final = _EXPECTED_SQL_HASHES_0027 | {
    # Derived from a real Alembic 0028 database with _canonical_schema_sql.
    (
        "table",
        "page_move_events",
    ): "1e7a4c37bcba5707237cdb8aff3fdf96c157dec0672f4b97904e37ba04a3707a",
    (
        "table",
        "page_move_guards",
    ): "bdf8bb2fe7d130eee0ce17f49abd64ae1a2acc48d00ac9ff41432729b9b9a7dd",
    (
        "table",
        "admin_master_move_receipts",
    ): "efbbd6f147f20f4d682c1850b89b5277de5ea6727529d3ce0366a2927bf03f6d",
    (
        "trigger",
        "trg_page_move_guards_validate_insert",
    ): "7b3bb41365b22e65a20551cdf7e13f5cbaefecc9ae8927a29dbaf937854da002",
    (
        "trigger",
        "trg_page_move_events_validate_insert",
    ): "1395ec23d9ffa047a80ba84a3a24f9bf36ea7bb42525dee59c18fb805bf6b113",
    (
        "trigger",
        "trg_page_move_guards_safe_delete",
    ): "144fb817b4f640229878675157b206dcd3783553cd9cebe94e65582af717de60",
    (
        "trigger",
        "trg_pages_move_require_guard",
    ): "b93006d7a983c5c2bfc417bd8c9f21da180a087f0a63b970a6dc13a258e28aa8",
    (
        "trigger",
        "trg_pages_move_record",
    ): "de568f34dd865f4f03a3a7a5da24f9da5b2bdeac2052736614d9e9ddefee7873",
    (
        "trigger",
        "trg_master_move_receipts_validate_insert",
    ): "b72526ebbffcd80b7b53bafa272d753ec38ab65e41cffee5e80f78711bae90ac",
    (
        "trigger",
        "trg_page_move_guards_no_update",
    ): "831a1132ce5712c9100d1c0ba452ff32fab6c40199e8bbb04e8a067db75d6eca",
    (
        "trigger",
        "trg_page_move_events_no_update",
    ): "d5002d52b3de55c94f3f427a5e0328ee74bc20f04413bb3065b9f19780d21e86",
    (
        "trigger",
        "trg_page_move_events_no_delete",
    ): "e862341bc3b126c7aaeca5ca8296a54854eb055df1d346d74ccc17b5b643f0fb",
    (
        "trigger",
        "trg_master_move_receipts_no_update",
    ): "2dd68ac75d5a6c34ce0f748ee900c3d49ee30448db41368220ce6d2bb62838b1",
    (
        "trigger",
        "trg_master_move_receipts_no_delete",
    ): "a4c4bf1d33ae1839a6e2b9c21ab2cd24ca5b75aa7a2fc0015d1f8ae704ce2135",
}

_EXPECTED_SQL_HASHES_0029: Final = _EXPECTED_SQL_HASHES_0028 | {
    (
        "table",
        "page_move_events",
    ): "5aeeafb8fc15a99ee8dc6aa076ad3c7a56743e7d742ce3648f68da6aced40bef",
    (
        "table",
        "page_move_guards",
    ): "c31db25abfef7d48a8872de03def115444263dedd28dae625e8d40be2dad089f",
    (
        "trigger",
        "trg_page_move_guards_validate_insert",
    ): "435bff8aee76d963f8f033e43321b57b32d7378728a0fbed9c0f9e642b55795d",
    (
        "trigger",
        "trg_page_move_events_validate_insert",
    ): "c8ab5e57ccc8fcde7d4b0e143c7e0b3ccadb7269ee18aa2d37ce5fd70adce4c6",
    (
        "trigger",
        "trg_page_move_guards_safe_delete",
    ): "a9d8679a7d67983717ae976711c3eb41346eb669ab513fb49c8695409715e687",
    (
        "trigger",
        "trg_pages_move_record",
    ): "13cc8f01fd8acfa1842f5fd563d088760cd21926361d09375066da7a48944a91",
}

_EXPECTED_SQL_HASHES_BY_REVISION: Final = {
    LEGACY_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0007,
    PREVIOUS_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0008,
    INTERMEDIATE_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0009,
    TAG_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0010,
    OCCURRENCE_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0011,
    LIFECYCLE_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0012,
    FILE_SET_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0013,
    LIBRARY_POLICY_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0014,
    AGENT_TOKEN_VALUES_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0015,
    MASTER_IDENTITY_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0016,
    ACTOR_HOME_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0017,
    MASTER_AUDIT_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0018,
    MASTER_LIFECYCLE_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0019,
    LIBRARY_DESCRIPTION_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0020,
    AUDIT_ACTOR_INDEX_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0021,
    PAGE_TITLE_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0022,
    REQUEST_LOG_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0023,
    SEARCH_INDEX_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0024,
    MASTER_PAGE_DELETE_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0025,
    MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0026,
    MASTER_OCCURRENCE_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0027,
    PAGE_MOVE_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0028,
    CALLER_PAGE_MOVE_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0029,
    LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION: _EXPECTED_SQL_HASHES_0029,
}
_FILE_SET_REVISIONS: Final = frozenset(
    {
        FILE_SET_SCHEMA_REVISION,
        LIBRARY_POLICY_SCHEMA_REVISION,
        AGENT_TOKEN_VALUES_SCHEMA_REVISION,
        MASTER_IDENTITY_SCHEMA_REVISION,
        ACTOR_HOME_SCHEMA_REVISION,
        MASTER_AUDIT_SCHEMA_REVISION,
        MASTER_LIFECYCLE_SCHEMA_REVISION,
        LIBRARY_DESCRIPTION_SCHEMA_REVISION,
        AUDIT_ACTOR_INDEX_SCHEMA_REVISION,
        PAGE_TITLE_SCHEMA_REVISION,
        REQUEST_LOG_SCHEMA_REVISION,
        SEARCH_INDEX_SCHEMA_REVISION,
        MASTER_PAGE_DELETE_SCHEMA_REVISION,
        MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION,
        MASTER_OCCURRENCE_SCHEMA_REVISION,
        PAGE_MOVE_SCHEMA_REVISION,
        CALLER_PAGE_MOVE_SCHEMA_REVISION,
        LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    }
)
_MASTER_LIFECYCLE_REVISIONS: Final = frozenset(
    {
        MASTER_LIFECYCLE_SCHEMA_REVISION,
        LIBRARY_DESCRIPTION_SCHEMA_REVISION,
        AUDIT_ACTOR_INDEX_SCHEMA_REVISION,
        PAGE_TITLE_SCHEMA_REVISION,
        REQUEST_LOG_SCHEMA_REVISION,
        SEARCH_INDEX_SCHEMA_REVISION,
        MASTER_PAGE_DELETE_SCHEMA_REVISION,
        MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION,
        MASTER_OCCURRENCE_SCHEMA_REVISION,
        PAGE_MOVE_SCHEMA_REVISION,
        CALLER_PAGE_MOVE_SCHEMA_REVISION,
        LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    }
)
_LIFECYCLE_REVISIONS: Final = _FILE_SET_REVISIONS | {LIFECYCLE_SCHEMA_REVISION}
_OCCURRENCE_REVISIONS: Final = _LIFECYCLE_REVISIONS | {OCCURRENCE_SCHEMA_REVISION}
_MASTER_OCCURRENCE_REVISIONS: Final = frozenset(
    {
        MASTER_OCCURRENCE_SCHEMA_REVISION,
        PAGE_MOVE_SCHEMA_REVISION,
        CALLER_PAGE_MOVE_SCHEMA_REVISION,
        LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    }
)


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
    without_rowid = normalized.endswith(" WITHOUT ROWID")
    if without_rowid:
        normalized = normalized[: -len(" WITHOUT ROWID")]
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
    result = f"{prefix} ({', '.join((*columns, *constraints))})"
    return f"{result} WITHOUT ROWID" if without_rowid else result


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


def _require_library_descriptions(connection: sqlite3.Connection) -> None:
    # SQLite affinity does not prevent a BLOB from occupying a TEXT column.
    for (description,) in connection.execute("SELECT description FROM libraries"):
        if type(description) is not str or len(description) > 4_000:
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
    master_column = (
        "master_audit_event_id" if schema_revision in _MASTER_OCCURRENCE_REVISIONS else "NULL"
    )
    chains: dict[tuple[str, bytes], list[tuple[int, int, int, int, int]]] = {}
    for (
        library_id,
        page_uid,
        sequence,
        old,
        new,
        at_revision,
        actor,
        corrected_at,
        master_audit_event_id,
    ) in connection.execute(
        "SELECT library_id, page_uid, sequence, old_occurred_at, new_occurred_at, "
        f"at_revision_number, actor_caller_id, corrected_at, {master_column} "
        "FROM page_occurrence_corrections ORDER BY library_id, page_uid, sequence"
    ):
        key = (library_id, page_uid)
        if (
            key not in pages
            or type(sequence) is not int
            or type(old) is not int
            or type(new) is not int
            or type(at_revision) is not int
            or type(corrected_at) is not int
        ):
            raise BackupDatabaseError
        if master_audit_event_id is not None:
            if (
                schema_revision not in _MASTER_OCCURRENCE_REVISIONS
                or type(master_audit_event_id) is not str
                or actor is not None
            ):
                raise BackupDatabaseError
            audit = connection.execute(
                "SELECT action, target_type, target_id, occurred_at "
                "FROM admin_master_audit_events WHERE id = ?",
                (master_audit_event_id,),
            ).fetchone()
            if audit != (
                "content.page.occurrence.correct",
                "page",
                f"{library_id}:{page_uid.hex()}",
                corrected_at,
            ):
                raise BackupDatabaseError
        elif type(actor) is not str:
            raise BackupDatabaseError
        chains.setdefault(key, []).append((sequence, old, new, at_revision, corrected_at))

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
        for expected_sequence, (sequence, old, new, at_revision, corrected_at) in enumerate(
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


def _require_lifecycle_graph(connection: sqlite3.Connection, schema_revision: str) -> None:
    """Rebuild each 0012 Page clock across Revisions, corrections, and trash events."""

    if _one_integer(connection, "SELECT count(*) FROM page_lifecycle_guards"):
        raise BackupDatabaseError

    pages: dict[tuple[str, bytes], tuple[str, str, str, str, int, int | None, int, int, int]] = {}
    for row in connection.execute(
        "SELECT library_id, page_uid, section_id, page_id, page_type, title, occurred_at, "
        "deleted_at, created_at, updated_at, current_revision_number FROM pages"
    ):
        (
            library_id,
            page_uid,
            section_id,
            page_id,
            page_type,
            title,
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
            or type(title) is not str
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
            title,
            occurred_at,
            deleted_at,
            created_at,
            updated_at,
            current_revision_number,
        )

    # Replay all changes to the Page clock, including metadata-only title edits.
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

    first_titles: dict[tuple[str, bytes], str] = {}
    if schema_revision in {
        PAGE_TITLE_SCHEMA_REVISION,
        REQUEST_LOG_SCHEMA_REVISION,
        SEARCH_INDEX_SCHEMA_REVISION,
        MASTER_PAGE_DELETE_SCHEMA_REVISION,
        MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION,
        MASTER_OCCURRENCE_SCHEMA_REVISION,
        PAGE_MOVE_SCHEMA_REVISION,
        CALLER_PAGE_MOVE_SCHEMA_REVISION,
        LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    }:
        for row in connection.execute(
            "SELECT library_id, page_uid, sequence, old_title, new_title, "
            "old_updated_at, changed_at, at_revision_number, master_audit_event_id "
            "FROM page_title_events ORDER BY library_id, page_uid, sequence"
        ):
            (
                library_id,
                page_uid,
                sequence,
                old_title,
                new_title,
                old_updated_at,
                changed_at,
                number,
                audit_id,
            ) = row
            key = (library_id, page_uid)
            if (
                key not in pages
                or type(sequence) is not int
                or type(old_title) is not str
                or type(new_title) is not str
                or old_title == new_title
                or type(old_updated_at) is not int
                or type(changed_at) is not int
                or type(number) is not int
                or type(audit_id) is not str
            ):
                raise BackupDatabaseError
            audit = connection.execute(
                "SELECT action, target_type, target_id, occurred_at "
                "FROM admin_master_audit_events WHERE id = ?",
                (audit_id,),
            ).fetchone()
            if audit != (
                "content.page.title.edit",
                "page",
                f"{library_id}:{page_uid.hex()}",
                changed_at,
            ):
                raise BackupDatabaseError
            first_titles.setdefault(key, old_title)
            operations.setdefault(key, []).append(
                (changed_at, "title", (sequence, old_title, new_title, old_updated_at, number))
            )

    master_column = (
        "master_audit_event_id" if schema_revision in _MASTER_LIFECYCLE_REVISIONS else "NULL"
    )
    for row in connection.execute(
        "SELECT library_id, page_uid, sequence, action, section_id, old_deleted_at, "
        "old_updated_at, changed_at, at_revision_number, occurred_at_at_event, "
        f"actor_caller_id, request_id, {master_column} FROM page_lifecycle_events "
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
            master_audit_event_id,
        ) = row
        key = (library_id, page_uid)
        if (
            key not in pages
            or type(sequence) is not int
            or action not in {"delete", "restore"}
            or pages[key][2] != "archive"
            or type(section_id) is not str
            or (old_deleted_at is not None and type(old_deleted_at) is not int)
            or type(old_updated_at) is not int
            or type(changed_at) is not int
            or type(number) is not int
            or type(occurrence) is not int
            or type(request_id) is not str
        ):
            raise BackupDatabaseError
        if master_audit_event_id is not None:
            if (
                schema_revision not in _MASTER_LIFECYCLE_REVISIONS
                or type(master_audit_event_id) is not str
                or actor is not None
                or (
                    action == "delete"
                    and schema_revision
                    not in {
                        MASTER_PAGE_DELETE_SCHEMA_REVISION,
                        MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION,
                        MASTER_OCCURRENCE_SCHEMA_REVISION,
                        PAGE_MOVE_SCHEMA_REVISION,
                        CALLER_PAGE_MOVE_SCHEMA_REVISION,
                        LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
                    }
                )
            ):
                raise BackupDatabaseError
            audit = connection.execute(
                "SELECT action, target_type, target_id, occurred_at "
                "FROM admin_master_audit_events WHERE id = ?",
                (master_audit_event_id,),
            ).fetchone()
            if audit != (
                f"content.archive.{action}",
                "page",
                f"{library_id}:{page_uid.hex()}",
                changed_at,
            ):
                raise BackupDatabaseError
        else:
            if type(actor) is not str:
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
            new_route = (
                lifecycle_route(action)[1]
                if schema_revision == LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION
                else route
            )
            replays = connection.execute(
                "SELECT response_body, route_template FROM idempotency_records "
                "WHERE library_id = ? "
                "AND caller_id = ? AND method = ? AND route_template IN (?, ?) "
                "AND original_request_id = ? AND original_request_timestamp = ?",
                (library_id, actor, method, route, new_route, request_id, timestamp),
            ).fetchall()
            if len(replays) != 1 or type(replays[0][0]) is not bytes:
                raise BackupDatabaseError
            try:
                replay_body = (
                    LibraryPageLifecycleBody.model_validate_json(replays[0][0])
                    if replays[0][1] in LIBRARY_LIFECYCLE_ROUTES
                    else PageLifecycleResponseBody.model_validate_json(replays[0][0])
                )
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
        final_title,
        final_occurrence,
        final_deleted_at,
        created_at,
        updated_at,
        final_revision,
    ) in pages.items():
        if schema_revision in {
            PAGE_MOVE_SCHEMA_REVISION,
            CALLER_PAGE_MOVE_SCHEMA_REVISION,
            LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
        }:
            _page_timeline(connection, schema_revision, key[0], key[1])
            continue
        first_revision = first_revisions.get(key)
        if first_revision is None or first_revision[1] != created_at:
            raise BackupDatabaseError
        revision_number = 1
        occurrence = first_occurrences.get(key, final_occurrence)
        title = first_titles.get(key, final_title)
        replayed_deleted_at: int | None = None
        prior_time = created_at
        correction_sequence = 0
        lifecycle_sequence = 0
        title_sequence = 0
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
            elif kind == "title":
                sequence, old, new, old_updated_at, number = values
                if (
                    replayed_deleted_at is not None
                    or sequence != title_sequence + 1
                    or old != title
                    or old_updated_at != prior_time
                    or number != revision_number
                    or new == old
                ):
                    raise BackupDatabaseError
                title_sequence = sequence
                title = new
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
            or final_title != title
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


def _require_agent_token_values(connection: sqlite3.Connection) -> None:
    """Check every revealable value against its exact, still-active Agent row.

    Expiry is deliberately not compared with the validation clock: a sound
    historical backup must not become invalid merely because time advanced.
    """

    rows = connection.execute(
        "SELECT v.credential_id, v.token_value, k.id, k.selector, "
        "k.token_version, k.verifier, k.revoked_at, k.rotated_at, "
        "a.kind, a.disabled_at FROM auth_agent_token_values AS v "
        "LEFT JOIN auth_credentials AS k ON k.id = v.credential_id "
        "LEFT JOIN auth_callers AS a ON a.id = k.caller_id "
        "AND a.library_id = k.library_id"
    )
    for (
        credential_id,
        token_value,
        stored_id,
        selector,
        token_version,
        verifier,
        revoked_at,
        rotated_at,
        kind,
        disabled_at,
    ) in rows:
        if (
            not isinstance(credential_id, str)
            or not isinstance(token_value, str)
            or stored_id != credential_id
            or kind != "agent"
            or disabled_at is not None
            or revoked_at is not None
            or rotated_at is not None
        ):
            raise BackupDatabaseError
        try:
            parsed = parse_token(token_value)
        except InvalidTokenError:
            raise BackupDatabaseError from None
        if (
            parsed.version != token_version
            or parsed.selector != selector
            or not verify_token(parsed, verifier)
        ):
            raise BackupDatabaseError


def _require_master_identity(connection: sqlite3.Connection) -> None:
    """Validate the optional verifier-only identity without exposing its value."""

    rows = connection.execute(
        "SELECT slot, identity_id, token_verifier, session_generation, created_at, updated_at "
        "FROM admin_master_identity"
    ).fetchall()
    if len(rows) > 1:
        raise BackupDatabaseError
    if not rows:
        return
    slot, identity_id, verifier, generation, created_at, updated_at = rows[0]
    if (
        slot != 1
        or not isinstance(identity_id, str)
        or len(identity_id) != 32
        or any(character not in "0123456789abcdef" for character in identity_id)
        or not isinstance(verifier, str)
        or type(generation) is not int
        or generation < 1
        or type(created_at) is not int
        or created_at < 0
        or type(updated_at) is not int
        or updated_at < created_at
    ):
        raise BackupDatabaseError
    try:
        parse_password_hash(verifier)
    except ValueError:
        raise BackupDatabaseError from None


def _require_master_audit(connection: sqlite3.Connection, schema_revision: str) -> None:
    """Validate immutable master action metadata without reading any secrets."""

    rows = connection.execute(
        "SELECT id, identity_id, session_generation, session_fingerprint, "
        "action, target_type, target_id, occurred_at FROM admin_master_audit_events"
    )
    for (
        event_id,
        identity_id,
        generation,
        fingerprint,
        action,
        target_type,
        target_id,
        occurred_at,
    ) in rows:
        if (
            not isinstance(event_id, str)
            or len(event_id) != 32
            or any(character not in "0123456789abcdef" for character in event_id)
            or not isinstance(identity_id, str)
            or len(identity_id) != 32
            or any(character not in "0123456789abcdef" for character in identity_id)
            or type(generation) is not int
            or generation < 1
            or not isinstance(fingerprint, bytes)
            or len(fingerprint) != 32
            or not isinstance(action, str)
            or not 1 <= len(action) <= 100
            or any(not 33 <= ord(character) <= 126 for character in action)
            or not isinstance(target_type, str)
            or not 1 <= len(target_type) <= 100
            or any(character not in "abcdefghijklmnopqrstuvwxyz_" for character in target_type)
            or not isinstance(target_id, str)
            or not 1 <= len(target_id) <= 200
            or any(not 33 <= ord(character) <= 126 for character in target_id)
            or type(occurred_at) is not int
            or occurred_at < 0
        ):
            raise BackupDatabaseError
        if action == "auth.agent_token.reveal" and (
            target_type != "credential"
            or len(target_id) != 32
            or any(character not in "0123456789abcdef" for character in target_id)
        ):
            raise BackupDatabaseError
        if action == "content.page.move":
            if schema_revision not in {
                PAGE_MOVE_SCHEMA_REVISION,
                CALLER_PAGE_MOVE_SCHEMA_REVISION,
                LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
            }:
                raise BackupDatabaseError
            linked = connection.execute(
                "SELECT e.library_id, e.page_uid, e.changed_at FROM page_move_events e "
                "JOIN admin_master_move_receipts r ON "
                "r.master_audit_event_id = e.master_audit_event_id "
                "WHERE e.master_audit_event_id = ? AND r.changed = 1 LIMIT 2",
                (event_id,),
            ).fetchall()
            if len(linked) != 1 or (
                target_type != "page"
                or target_id != f"{linked[0][0]}:{linked[0][1].hex()}"
                or occurred_at != linked[0][2]
            ):
                raise BackupDatabaseError
        if action == "content.page.occurrence.correct":
            if schema_revision not in _MASTER_OCCURRENCE_REVISIONS:
                raise BackupDatabaseError
            linked_events = connection.execute(
                "SELECT library_id, page_uid, corrected_at FROM page_occurrence_corrections "
                "WHERE master_audit_event_id = ? LIMIT 2",
                (event_id,),
            ).fetchall()
            if len(linked_events) != 1:
                raise BackupDatabaseError
            library_id, page_uid, corrected_at = linked_events[0]
            if (
                type(library_id) is not str
                or type(page_uid) is not bytes
                or type(corrected_at) is not int
                or target_type != "page"
                or target_id != f"{library_id}:{page_uid.hex()}"
                or occurred_at != corrected_at
            ):
                raise BackupDatabaseError
        if schema_revision in _MASTER_LIFECYCLE_REVISIONS and action in {
            "content.archive.restore",
            "content.archive.delete",
        }:
            if action == "content.archive.delete" and schema_revision not in {
                MASTER_PAGE_DELETE_SCHEMA_REVISION,
                MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION,
                MASTER_OCCURRENCE_SCHEMA_REVISION,
                PAGE_MOVE_SCHEMA_REVISION,
                CALLER_PAGE_MOVE_SCHEMA_REVISION,
                LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
            }:
                raise BackupDatabaseError
            linked_events = connection.execute(
                "SELECT library_id, page_uid, changed_at, action FROM page_lifecycle_events "
                "WHERE master_audit_event_id = ? LIMIT 2",
                (event_id,),
            ).fetchall()
            if len(linked_events) != 1:
                raise BackupDatabaseError
            library_id, page_uid, changed_at, lifecycle_action = linked_events[0]
            if (
                type(library_id) is not str
                or type(page_uid) is not bytes
                or type(changed_at) is not int
                or target_type != "page"
                or target_id != f"{library_id}:{page_uid.hex()}"
                or occurred_at != changed_at
                or action != f"content.archive.{lifecycle_action}"
            ):
                raise BackupDatabaseError
        if (
            schema_revision
            in {
                PAGE_TITLE_SCHEMA_REVISION,
                REQUEST_LOG_SCHEMA_REVISION,
                SEARCH_INDEX_SCHEMA_REVISION,
                MASTER_PAGE_DELETE_SCHEMA_REVISION,
                MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION,
                MASTER_OCCURRENCE_SCHEMA_REVISION,
                PAGE_MOVE_SCHEMA_REVISION,
                CALLER_PAGE_MOVE_SCHEMA_REVISION,
                LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
            }
            and action == "content.page.title.edit"
        ):
            linked_events = connection.execute(
                "SELECT library_id, page_uid, changed_at FROM page_title_events "
                "WHERE master_audit_event_id = ? LIMIT 2",
                (event_id,),
            ).fetchall()
            if len(linked_events) != 1:
                raise BackupDatabaseError
            library_id, page_uid, changed_at = linked_events[0]
            if (
                type(library_id) is not str
                or type(page_uid) is not bytes
                or type(changed_at) is not int
                or target_type != "page"
                or target_id != f"{library_id}:{page_uid.hex()}"
                or occurred_at != changed_at
            ):
                raise BackupDatabaseError


def _require_actor_home_graph(connection: sqlite3.Connection, schema_revision: str) -> None:
    """Keep each history row's content target separate from its actor identity.

    SQLite's foreign-key check proves the declared relationships. These joins
    also pin the domain meaning of the new home field and reject history whose
    target Library or acting caller cannot be resolved independently.
    """

    for table, actor_column in (
        ("auth_audit_events", "actor_caller_id"),
        ("idempotency_records", "caller_id"),
        ("page_occurrence_corrections", "actor_caller_id"),
        ("page_occurrence_correction_guards", "actor_caller_id"),
        ("page_lifecycle_events", "actor_caller_id"),
        ("page_lifecycle_guards", "actor_caller_id"),
    ):
        master_lifecycle = schema_revision in _MASTER_LIFECYCLE_REVISIONS and table in {
            "page_lifecycle_events",
            "page_lifecycle_guards",
        }
        master_occurrence = schema_revision in _MASTER_OCCURRENCE_REVISIONS and table in {
            "page_occurrence_corrections",
            "page_occurrence_correction_guards",
        }
        if master_lifecycle or master_occurrence:
            invalid_predicate = (
                "target.id IS NULL OR "
                "(history.master_audit_event_id IS NULL AND actor.id IS NULL) OR "
                "(history.master_audit_event_id IS NOT NULL AND "
                "(history.actor_caller_id IS NOT NULL OR "
                "history.actor_home_library_id IS NOT NULL))"
            )
        else:
            invalid_predicate = "target.id IS NULL OR actor.id IS NULL"
        invalid = _one_integer(
            connection,
            f"SELECT count(*) FROM {table} AS history "
            "LEFT JOIN libraries AS target ON target.id = history.library_id "
            f"LEFT JOIN auth_callers AS actor ON actor.id = history.{actor_column} "
            "AND actor.library_id = history.actor_home_library_id "
            f"WHERE {invalid_predicate}",
        )
        if invalid:
            raise BackupDatabaseError

    invalid_audit_credentials = _one_integer(
        connection,
        "SELECT count(*) FROM auth_audit_events AS event "
        "LEFT JOIN auth_credentials AS credential "
        "ON credential.id = event.actor_credential_id "
        "AND credential.caller_id = event.actor_caller_id "
        "AND credential.library_id = event.actor_home_library_id "
        "WHERE credential.id IS NULL",
    )
    if invalid_audit_credentials:
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

    if schema_revision in {
        LIBRARY_POLICY_SCHEMA_REVISION,
        AGENT_TOKEN_VALUES_SCHEMA_REVISION,
        MASTER_IDENTITY_SCHEMA_REVISION,
        ACTOR_HOME_SCHEMA_REVISION,
        MASTER_AUDIT_SCHEMA_REVISION,
        MASTER_LIFECYCLE_SCHEMA_REVISION,
        LIBRARY_DESCRIPTION_SCHEMA_REVISION,
        AUDIT_ACTOR_INDEX_SCHEMA_REVISION,
        PAGE_TITLE_SCHEMA_REVISION,
        REQUEST_LOG_SCHEMA_REVISION,
        SEARCH_INDEX_SCHEMA_REVISION,
        MASTER_PAGE_DELETE_SCHEMA_REVISION,
        MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION,
        MASTER_OCCURRENCE_SCHEMA_REVISION,
        PAGE_MOVE_SCHEMA_REVISION,
        CALLER_PAGE_MOVE_SCHEMA_REVISION,
        LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    }:
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

    if schema_revision in {
        AGENT_TOKEN_VALUES_SCHEMA_REVISION,
        MASTER_IDENTITY_SCHEMA_REVISION,
        ACTOR_HOME_SCHEMA_REVISION,
        MASTER_AUDIT_SCHEMA_REVISION,
        MASTER_LIFECYCLE_SCHEMA_REVISION,
        LIBRARY_DESCRIPTION_SCHEMA_REVISION,
        AUDIT_ACTOR_INDEX_SCHEMA_REVISION,
        PAGE_TITLE_SCHEMA_REVISION,
        REQUEST_LOG_SCHEMA_REVISION,
        SEARCH_INDEX_SCHEMA_REVISION,
        MASTER_PAGE_DELETE_SCHEMA_REVISION,
        MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION,
        MASTER_OCCURRENCE_SCHEMA_REVISION,
        PAGE_MOVE_SCHEMA_REVISION,
        CALLER_PAGE_MOVE_SCHEMA_REVISION,
        LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    }:
        _require_agent_token_values(connection)

    if schema_revision in {
        MASTER_IDENTITY_SCHEMA_REVISION,
        ACTOR_HOME_SCHEMA_REVISION,
        MASTER_AUDIT_SCHEMA_REVISION,
        MASTER_LIFECYCLE_SCHEMA_REVISION,
        LIBRARY_DESCRIPTION_SCHEMA_REVISION,
        AUDIT_ACTOR_INDEX_SCHEMA_REVISION,
        PAGE_TITLE_SCHEMA_REVISION,
        REQUEST_LOG_SCHEMA_REVISION,
        SEARCH_INDEX_SCHEMA_REVISION,
        MASTER_PAGE_DELETE_SCHEMA_REVISION,
        MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION,
        MASTER_OCCURRENCE_SCHEMA_REVISION,
        PAGE_MOVE_SCHEMA_REVISION,
        CALLER_PAGE_MOVE_SCHEMA_REVISION,
        LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    }:
        _require_master_identity(connection)

    if schema_revision in {
        ACTOR_HOME_SCHEMA_REVISION,
        MASTER_AUDIT_SCHEMA_REVISION,
        MASTER_LIFECYCLE_SCHEMA_REVISION,
        LIBRARY_DESCRIPTION_SCHEMA_REVISION,
        AUDIT_ACTOR_INDEX_SCHEMA_REVISION,
        PAGE_TITLE_SCHEMA_REVISION,
        REQUEST_LOG_SCHEMA_REVISION,
        SEARCH_INDEX_SCHEMA_REVISION,
        MASTER_PAGE_DELETE_SCHEMA_REVISION,
        MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION,
        MASTER_OCCURRENCE_SCHEMA_REVISION,
        PAGE_MOVE_SCHEMA_REVISION,
        CALLER_PAGE_MOVE_SCHEMA_REVISION,
        LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    }:
        _require_actor_home_graph(connection, schema_revision)

    if schema_revision in {
        MASTER_AUDIT_SCHEMA_REVISION,
        MASTER_LIFECYCLE_SCHEMA_REVISION,
        LIBRARY_DESCRIPTION_SCHEMA_REVISION,
        AUDIT_ACTOR_INDEX_SCHEMA_REVISION,
        PAGE_TITLE_SCHEMA_REVISION,
        REQUEST_LOG_SCHEMA_REVISION,
        SEARCH_INDEX_SCHEMA_REVISION,
        MASTER_PAGE_DELETE_SCHEMA_REVISION,
        MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION,
        MASTER_OCCURRENCE_SCHEMA_REVISION,
        PAGE_MOVE_SCHEMA_REVISION,
        CALLER_PAGE_MOVE_SCHEMA_REVISION,
        LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    }:
        _require_master_audit(connection, schema_revision)

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
    schema_revision: str,
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
        "WHERE p.library_id = ? AND e.section_id = ? AND p.page_id = ? "
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
    if schema_revision in {
        PAGE_MOVE_SCHEMA_REVISION,
        CALLER_PAGE_MOVE_SCHEMA_REVISION,
        LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    }:
        state = _page_timeline(connection, schema_revision, library_id, page_uid).exact_state(
            changed_at
        )
        if state.section_id != body.section_id:
            raise BackupDatabaseError
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
    filename: str
    size_bytes: int = Field(ge=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


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
    schema_revision: str,
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
    if schema_revision in {
        PAGE_TITLE_SCHEMA_REVISION,
        REQUEST_LOG_SCHEMA_REVISION,
        SEARCH_INDEX_SCHEMA_REVISION,
        MASTER_PAGE_DELETE_SCHEMA_REVISION,
        MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION,
        MASTER_OCCURRENCE_SCHEMA_REVISION,
        PAGE_MOVE_SCHEMA_REVISION,
        CALLER_PAGE_MOVE_SCHEMA_REVISION,
        LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    }:
        for (changed_at,) in connection.execute(
            "SELECT changed_at FROM page_title_events "
            "WHERE library_id = ? AND page_uid = ? AND at_revision_number = ?",
            (library_id, page_uid, revision_number),
        ):
            if type(changed_at) is not int:
                raise BackupDatabaseError
            events.append((changed_at, "title", 0))
    active = True
    for changed_at, kind, value in sorted(events, key=lambda event: event[0]):
        if changed_at <= revision_at:
            raise BackupDatabaseError
        if kind == "correction":
            if type(value) is not int:
                raise BackupDatabaseError
            occurrence = value
        elif kind == "lifecycle":
            active = value == "restore"
        if active:
            valid.add(
                page_current_etag(page_uid, revision_id, revision_number, occurrence, changed_at)
            )
    return valid


def _require_file_set_replay(
    connection: sqlite3.Connection,
    *,
    schema_revision: str,
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
        "WHERE p.library_id = ? AND (p.section_id = ? OR ? = 1) AND p.page_id = ? "
        "AND r.revision_id = ? AND r.revision_number = ?",
        (
            library_id,
            body.section_id,
            int(
                schema_revision
                in {
                    PAGE_MOVE_SCHEMA_REVISION,
                    CALLER_PAGE_MOVE_SCHEMA_REVISION,
                    LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
                }
            ),
            body.page_id,
            body.revision_id,
            body.revision_number,
        ),
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
    timeline = None
    if schema_revision in {
        PAGE_MOVE_SCHEMA_REVISION,
        CALLER_PAGE_MOVE_SCHEMA_REVISION,
        LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    }:
        timeline = _page_timeline(connection, schema_revision, library_id, page_uid)
        state = timeline.match_active_etag(
            revision_id=body.revision_id,
            revision_number=body.revision_number,
            etag=etag,
            section_id=body.section_id,
            book_id=body.book_id if isinstance(body, _FileSetCreateReplayBody) else None,
        )
        if (is_create or isinstance(body, _FileSetAppendReplayBody) and body.changed) and (
            state.updated_at != revision_at
        ):
            raise BackupDatabaseError
        book_id = state.book_id
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
    response_files = [
        (entry.filename, entry.size_bytes, entry.content_sha256) for entry in body.files
    ]
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
            if timeline is None and etag not in _file_set_valid_current_etags(
                connection,
                schema_revision=schema_revision,
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


def _page_title_at(
    connection: sqlite3.Connection,
    *,
    schema_revision: str,
    library_id: str,
    page_uid: bytes,
    current_title: str,
    at: int,
) -> str:
    if schema_revision not in {
        PAGE_TITLE_SCHEMA_REVISION,
        REQUEST_LOG_SCHEMA_REVISION,
        SEARCH_INDEX_SCHEMA_REVISION,
        MASTER_PAGE_DELETE_SCHEMA_REVISION,
        MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION,
        MASTER_OCCURRENCE_SCHEMA_REVISION,
        PAGE_MOVE_SCHEMA_REVISION,
        CALLER_PAGE_MOVE_SCHEMA_REVISION,
        LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    }:
        return current_title
    row = connection.execute(
        "SELECT old_title FROM page_title_events "
        "WHERE library_id = ? AND page_uid = ? AND changed_at > ? "
        "ORDER BY changed_at LIMIT 1",
        (library_id, page_uid, at),
    ).fetchone()
    return current_title if row is None else row[0]


def _require_idempotency_graph(connection: sqlite3.Connection, schema_revision: str) -> None:
    if schema_revision == LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION:
        try:
            require_library_lifecycle_graph(connection, schema_revision=schema_revision)
        except (ValueError, RuntimeError, sqlite3.Error):
            raise BackupDatabaseError from None
    elif connection.execute(
        "SELECT 1 FROM auth_audit_events WHERE action IN "
        "('content.page.delete','content.page.restore') LIMIT 1"
    ).fetchone():
        raise BackupDatabaseError

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

        if route in LIBRARY_LIFECYCLE_ROUTES:
            if schema_revision != LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION:
                raise BackupDatabaseError
            # Full bidirectional proof was checked above, including exact bytes.
            continue

        if route == "/api/v1/libraries/{library_id}/pages/{page_id}/move":
            if schema_revision not in {
                CALLER_PAGE_MOVE_SCHEMA_REVISION,
                LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
            }:
                raise BackupDatabaseError
            continue

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
                "WHERE p.library_id = ? AND (p.section_id = ? OR ? = 1) AND p.page_id = ? "
                "AND c.corrected_at = ? LIMIT 2",
                (
                    library_id,
                    correction_body.section_id,
                    int(
                        schema_revision
                        in {
                            PAGE_MOVE_SCHEMA_REVISION,
                            CALLER_PAGE_MOVE_SCHEMA_REVISION,
                            LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
                        }
                    ),
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
            if schema_revision in {
                PAGE_MOVE_SCHEMA_REVISION,
                CALLER_PAGE_MOVE_SCHEMA_REVISION,
                LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
            }:
                state = _page_timeline(
                    connection, schema_revision, library_id, page_uid
                ).exact_state(corrected_at)
                if state.section_id != correction_body.section_id or state.deleted_at is not None:
                    raise BackupDatabaseError
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
                schema_revision=schema_revision,
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
                schema_revision=schema_revision,
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
            "WHERE p.library_id = ? AND p.page_id = ? AND (p.section_id = ? OR ? = 1)",
            (
                body.revision.revision_id,
                body.revision.revision_number,
                library_id,
                body.page.page_id,
                body.page.section_id,
                int(
                    schema_revision
                    in {
                        PAGE_MOVE_SCHEMA_REVISION,
                        CALLER_PAGE_MOVE_SCHEMA_REVISION,
                        LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
                    }
                ),
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
        historical_book = row[1]
        response_occurrence = row[4]
        if schema_revision in {
            PAGE_MOVE_SCHEMA_REVISION,
            CALLER_PAGE_MOVE_SCHEMA_REVISION,
            LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
        }:
            state = _page_timeline(connection, schema_revision, library_id, row[0]).exact_state(
                row[5]
            )
            if state.section_id != body.page.section_id or state.deleted_at is not None:
                raise BackupDatabaseError
            historical_book = state.book_id
            response_occurrence = state.occurred_at
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
            body.page.book_id != historical_book
            or body.page.title
            != _page_title_at(
                connection,
                schema_revision=schema_revision,
                library_id=library_id,
                page_uid=row[0],
                current_title=row[2],
                at=row[5],
            )
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


def _require_request_log_graph(connection: sqlite3.Connection) -> None:
    """Validate the complete disposable metadata table, including SQLite value types."""

    for row in connection.execute(
        "SELECT id, request_id, method, route_template, status_code, completion, "
        "occurred_at, duration_us, caller_id, home_library_id, credential_id "
        "FROM api_request_log ORDER BY id"
    ):
        (
            row_id,
            request_id,
            method,
            route_template,
            status_code,
            completion,
            occurred_at,
            duration_us,
            caller_id,
            home_library_id,
            credential_id,
        ) = row
        if type(row_id) is not int or row_id < 1:
            raise BackupDatabaseError
        try:
            RequestLogWrite(
                request_id=request_id,
                method=method,
                route_template=route_template,
                status_code=status_code,
                completion=completion,
                occurred_at=occurred_at,
                duration_us=duration_us,
                caller_id=caller_id,
                home_library_id=home_library_id,
                credential_id=credential_id,
            )
        except (TypeError, ValueError):
            raise BackupDatabaseError from None


def _require_search_projection_reset(connection: sqlite3.Connection) -> None:
    """A portable backup contains authority, not an apparently ready search cache."""

    rows = connection.execute(
        "SELECT active_generation, ready, dirty_sequence, index_version FROM search_meta"
    ).fetchall()
    if (
        len(rows) != 1
        or rows[0][0] is not None
        or rows[0][1] != 0
        or type(rows[0][2]) is not int
        or rows[0][2] < 0
        or type(rows[0][3]) is not str
        or not rows[0][3]
    ):
        raise BackupDatabaseError
    for table in (
        "search_generations",
        "search_page_state",
        "search_documents",
        "search_terms",
    ):
        if _one_integer(connection, f"SELECT count(*) FROM {table}") != 0:
            raise BackupDatabaseError
    for library_id, page_uid, seq in connection.execute(
        "SELECT library_id, page_uid, seq FROM search_dirty_pages"
    ):
        if (
            type(library_id) is not str
            or type(page_uid) is not bytes
            or len(page_uid) != 16
            or type(seq) is not int
            or not 1 <= seq <= rows[0][2]
            or connection.execute(
                "SELECT count(*) FROM pages WHERE library_id = ? AND page_uid = ?",
                (library_id, page_uid),
            ).fetchone()
            != (1,)
        ):
            raise BackupDatabaseError


def _page_timeline(
    connection: sqlite3.Connection, schema_revision: str, library_id: str, page_uid: bytes
) -> PageStateTimeline:
    try:
        return load_page_state_timeline(
            connection, schema_revision=schema_revision, library_id=library_id, page_uid=page_uid
        )
    except PageMembershipHistoryError:
        raise BackupDatabaseError from None


def _require_caller_move_graph(connection: sqlite3.Connection, schema_revision: str) -> None:
    from collections import Counter

    from patchouli_lib.content.page_move_receipts import validate_caller_move_receipt
    from patchouli_lib.content.page_move_schemas import PAGE_MOVE_ROUTE_TEMPLATE
    from patchouli_lib.idempotency.schemas import StoredIdempotencyRecord

    successes: Counter[tuple[str, str, int]] = Counter()
    cursor = connection.execute(
        "SELECT * FROM idempotency_records WHERE route_template = ?", (PAGE_MOVE_ROUTE_TEMPLATE,)
    )
    names = [column[0] for column in cursor.description]
    try:
        for row in cursor:
            record = StoredIdempotencyRecord.model_validate(dict(zip(names, row, strict=True)))
            body = validate_caller_move_receipt(connection, record, schema_revision=schema_revision)
            if body.changed:
                successes[
                    (
                        body.library_id,
                        body.page_id,
                        parse_occurrence_time(body.updated_at).utc_microseconds,
                    )
                ] += 1
        events = Counter(
            connection.execute(
                "SELECT e.library_id, p.page_id, e.changed_at FROM page_move_events e "
                "JOIN pages p ON p.library_id=e.library_id AND p.page_uid=e.page_uid "
                "WHERE e.caller_audit_event_id IS NOT NULL"
            ).fetchall()
        )
        if events != successes or any(count != 1 for count in successes.values()):
            raise ValueError
        if connection.execute(
            "SELECT 1 FROM auth_audit_events a LEFT JOIN page_move_events e "
            "ON e.caller_audit_event_id=a.id WHERE a.action='content.page.move' "
            "AND e.caller_audit_event_id IS NULL LIMIT 1"
        ).fetchone():
            raise ValueError
    except (ValueError, RuntimeError, sqlite3.Error):
        raise BackupDatabaseError from None


def _require_page_moves(
    connection: sqlite3.Connection, *, schema_revision: str = PAGE_MOVE_SCHEMA_REVISION
) -> None:
    if _one_integer(connection, "SELECT count(*) FROM page_move_guards"):
        raise BackupDatabaseError
    # Both reverse directions are mandatory: an event without its frozen success
    # and an orphan move audit cannot be accepted merely because FKs pass.
    if connection.execute(
        "SELECT 1 FROM page_move_events e LEFT JOIN admin_master_move_receipts r "
        "ON r.library_id = e.library_id AND r.page_uid = e.page_uid AND "
        "r.move_sequence = e.sequence "
        "WHERE e.master_audit_event_id IS NOT NULL AND "
        "(r.move_sequence IS NULL OR r.changed != 1) LIMIT 1"
    ).fetchone():
        raise BackupDatabaseError
    if schema_revision in {
        CALLER_PAGE_MOVE_SCHEMA_REVISION,
        LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    }:
        _require_caller_move_graph(connection, schema_revision)
    cursor = connection.execute("SELECT * FROM admin_master_move_receipts")
    names = [item[0] for item in cursor.description]
    for row in cursor:
        try:
            receipt = MasterMoveReceipt.model_validate(dict(zip(names, row, strict=True)))
            validate_master_move_receipt(connection, receipt, schema_revision=schema_revision)
        except (ValueError, MasterMoveReceiptCorruptError):
            raise BackupDatabaseError from None


def _require_master_file_set_receipts(connection: sqlite3.Connection) -> None:
    cursor = connection.execute("SELECT * FROM admin_master_file_set_receipts")
    names = [column[0] for column in cursor.description]
    try:
        for row in cursor:
            receipt = MasterFileSetReceipt.model_validate(dict(zip(names, row, strict=True)))
            validate_master_file_set_receipt(connection, receipt)
    except (ValueError, MasterFileSetReceiptCorruptError):
        raise BackupDatabaseError from None
    # Every new master content audit must describe exactly one changed success.
    # No-op and replay deliberately create neither Source nor content activity.
    if connection.execute(
        "SELECT 1 FROM admin_master_audit_events a "
        "LEFT JOIN admin_master_file_set_receipts r ON r.master_audit_event_id = a.id "
        "WHERE a.action IN ('content.page.file_set.create', 'content.page.file_set.revise') "
        "AND r.master_audit_event_id IS NULL LIMIT 1"
    ).fetchone():
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
    if schema_revision in {
        LIBRARY_DESCRIPTION_SCHEMA_REVISION,
        AUDIT_ACTOR_INDEX_SCHEMA_REVISION,
        PAGE_TITLE_SCHEMA_REVISION,
        REQUEST_LOG_SCHEMA_REVISION,
        SEARCH_INDEX_SCHEMA_REVISION,
        MASTER_PAGE_DELETE_SCHEMA_REVISION,
        MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION,
        MASTER_OCCURRENCE_SCHEMA_REVISION,
        PAGE_MOVE_SCHEMA_REVISION,
        CALLER_PAGE_MOVE_SCHEMA_REVISION,
        LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    }:
        _require_library_descriptions(connection)
    if schema_revision in _LIFECYCLE_REVISIONS:
        _require_lifecycle_graph(connection, schema_revision)
    if schema_revision not in {
        LEGACY_SCHEMA_REVISION,
        PREVIOUS_SCHEMA_REVISION,
        INTERMEDIATE_SCHEMA_REVISION,
    }:
        _require_tag_graph(connection)
    _require_auth_graph(connection, schema_revision)
    try:
        _require_idempotency_graph(connection, schema_revision)
    except PageMembershipHistoryError:
        raise BackupDatabaseError from None
    if schema_revision in {
        PAGE_MOVE_SCHEMA_REVISION,
        CALLER_PAGE_MOVE_SCHEMA_REVISION,
        LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    }:
        _require_page_moves(connection, schema_revision=schema_revision)
    if schema_revision in {
        MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION,
        MASTER_OCCURRENCE_SCHEMA_REVISION,
        PAGE_MOVE_SCHEMA_REVISION,
        CALLER_PAGE_MOVE_SCHEMA_REVISION,
        LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    }:
        _require_master_file_set_receipts(connection)
    if schema_revision in {
        REQUEST_LOG_SCHEMA_REVISION,
        SEARCH_INDEX_SCHEMA_REVISION,
        MASTER_PAGE_DELETE_SCHEMA_REVISION,
        MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION,
        MASTER_OCCURRENCE_SCHEMA_REVISION,
        PAGE_MOVE_SCHEMA_REVISION,
        CALLER_PAGE_MOVE_SCHEMA_REVISION,
        LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    }:
        _require_request_log_graph(connection)
    if schema_revision in {
        SEARCH_INDEX_SCHEMA_REVISION,
        MASTER_PAGE_DELETE_SCHEMA_REVISION,
        MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION,
        MASTER_OCCURRENCE_SCHEMA_REVISION,
        PAGE_MOVE_SCHEMA_REVISION,
        CALLER_PAGE_MOVE_SCHEMA_REVISION,
        LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    }:
        _require_search_projection_reset(connection)
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

from sqlalchemy import Engine

from patchouli_lib.retrieval.repository import RetrievalRepository
from patchouli_lib.retrieval.schemas import ReadWindow

from .conftest import RetrievalScope


def test_repository_keeps_caller_owned_transaction_active(
    retrieval_engine: Engine,
    retrieval_scope: RetrievalScope,
) -> None:
    with retrieval_engine.connect() as connection:
        transaction = connection.begin()
        page = RetrievalRepository(connection).list_pages(
            retrieval_scope.library_id,
            retrieval_scope.query_section_id,
            ReadWindow(limit=1),
        )
        assert transaction.is_active
        assert connection.in_transaction()
        assert len(page.items) == 1
        transaction.rollback()


def test_repository_keyset_is_bounded_deterministic_and_hides_tombstones(
    retrieval_engine: Engine,
    retrieval_scope: RetrievalScope,
) -> None:
    with retrieval_engine.connect() as connection:
        repository = RetrievalRepository(connection)
        first = repository.list_pages(
            retrieval_scope.library_id,
            retrieval_scope.query_section_id,
            ReadWindow(limit=1),
        )
        assert tuple(item.page_id for item in first.items) == (retrieval_scope.first_page_id,)
        assert first.next_key == retrieval_scope.first_page_id

        second = repository.list_pages(
            retrieval_scope.library_id,
            retrieval_scope.query_section_id,
            ReadWindow(limit=1, after_key=first.next_key),
        )
        assert tuple(item.page_id for item in second.items) == (retrieval_scope.second_page_id,)
        assert second.next_key is None
        assert retrieval_scope.deleted_page_id not in {
            item.page_id for item in (*first.items, *second.items)
        }

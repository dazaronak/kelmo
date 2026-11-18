"""The journal reads events by operation; an index on (operation_id, stage) keeps that read from scanning every
event, in a new journal and in one created before the index existed."""
from kelmo.journal import Journal

STAGES = 'SELECT DISTINCT stage FROM events WHERE operation_id=?'


def plan(journal):
    return ' '.join(row[3] for row in journal.conn.execute('EXPLAIN QUERY PLAN ' + STAGES, ('x',)))


def test_a_new_journal_reads_an_operations_events_through_an_index(tmp_path):
    journal = Journal(tmp_path / 'journal.sqlite3', create=True)
    assert 'USING' in plan(journal) and 'INDEX' in plan(journal), plan(journal)


def test_a_journal_from_before_the_index_gets_it_when_a_writer_opens_it(tmp_path):
    path = tmp_path / 'journal.sqlite3'
    old = Journal(path, create=True)
    with old.conn:
        for (name,) in old.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events'").fetchall():
            old.conn.execute(f'DROP INDEX "{name}"')
    assert 'SCAN' in plan(old)
    old.conn.close()

    reopened = Journal(path)

    assert 'USING' in plan(reopened) and 'INDEX' in plan(reopened), plan(reopened)


def test_a_reader_opens_a_journal_from_before_the_index_without_writing(tmp_path):
    path = tmp_path / 'journal.sqlite3'
    old = Journal(path, create=True)
    with old.conn:
        for (name,) in old.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events'").fetchall():
            old.conn.execute(f'DROP INDEX "{name}"')
    old.conn.close()

    reader = Journal(path, read_only=True)

    assert reader.conn.execute(STAGES, ('x',)).fetchall() == []


ARCHIVE = 'SELECT id FROM operations WHERE archive_id=? ORDER BY created_at, id'


def archive_plan(journal):
    return ' '.join(row[3] for row in journal.conn.execute('EXPLAIN QUERY PLAN ' + ARCHIVE, ('x',)))


def test_a_record_s_operations_are_found_through_an_index(tmp_path):
    """`compat_metadata`, run for every search hit and list item, finds a record's operations by archive id."""
    journal = Journal(tmp_path / 'journal.sqlite3', create=True)
    assert 'USING' in archive_plan(journal) and 'INDEX' in archive_plan(journal), archive_plan(journal)


def test_a_journal_from_before_the_archive_index_gets_it_when_a_writer_opens_it(tmp_path):
    path = tmp_path / 'journal.sqlite3'
    old = Journal(path, create=True)
    with old.conn:
        for (name,) in old.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='operations' "
                "AND name NOT LIKE 'sqlite_autoindex%'").fetchall():
            old.conn.execute(f'DROP INDEX "{name}"')
    assert 'SCAN' in archive_plan(old)
    old.conn.close()

    reopened = Journal(path)

    assert 'USING' in archive_plan(reopened) and 'INDEX' in archive_plan(reopened), archive_plan(reopened)

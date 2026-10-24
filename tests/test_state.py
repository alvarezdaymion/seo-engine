import pytest

from seo_engine.state import State, WriteJournal


@pytest.mark.parametrize("content", ['{"page":', '[]', '"bad state"', '{"page": null}', 'null'])
def test_corrupt_page_state_is_preserved_and_blocks_loading(tmp_path, content):
    path = tmp_path / "pages.json"
    path.write_text(content)
    with pytest.raises(RuntimeError):
        State(tmp_path)
    assert path.read_text() == content


@pytest.mark.parametrize("content", ['{"operation":', '[]'])
def test_corrupt_write_journal_is_preserved(tmp_path, content):
    path = tmp_path / "writes.json"
    path.write_text(content)
    with pytest.raises(RuntimeError):
        WriteJournal(path)
    assert path.read_text() == content


@pytest.mark.parametrize("content", ["[]", "{}", '{"slug": "page"}'])
def test_malformed_saved_build_is_preserved(tmp_path, content):
    path = tmp_path / "pages" / "page.pending.json"
    path.parent.mkdir()
    path.write_text(content)
    with pytest.raises(RuntimeError):
        State(tmp_path).page("page", pending=True)
    assert path.read_text() == content


def test_unreadable_ledger_is_not_treated_as_no_activity(tmp_path):
    (tmp_path / "ledger.jsonl").mkdir()
    with pytest.raises(RuntimeError):
        State(tmp_path).events()

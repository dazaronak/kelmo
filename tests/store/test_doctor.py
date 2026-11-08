"""The dependency checks behind the administrator doctor."""
from kelmo.store.doctor import dependency_checks


def by_name(results, name):
    return next(r for r in results if r["name"] == name)


def test_doctor_returns_list_of_check_results():
    results = dependency_checks()
    assert isinstance(results, list)
    assert len(results) >= 4
    for r in results:
        assert "name" in r
        assert "status" in r
        assert r["status"] in ("PASS", "FAIL", "WARN", "SKIP")


def test_doctor_local_libraries_pass():
    results = dependency_checks()
    for name in ("sqlite_vec_loads", "fts5_available", "schema_valid", "taxonomy_valid"):
        assert by_name(results, name)["status"] == "PASS", name


def test_doctor_probes_left_out_are_skipped_not_passed():
    results = dependency_checks()
    assert by_name(results, "embedding_service")["status"] == "SKIP"
    assert by_name(results, "tokenizer_present")["status"] == "SKIP"

import pytest

from tests.owner import owner_plain


@pytest.fixture(scope="session", autouse=True)
def keep_audit_log_clean():
    """Tests hit real routes, which write audit rows. Remove the ones this run wrote.

    (Only the owner can delete; the app role is insert-only. Don't run the suite
    against a server whose audit log you need to keep.)
    """
    start = owner_plain("SELECT coalesce(max(id), 0) AS n FROM audit_log", fetch=True)[0]["n"]
    yield
    owner_plain("DELETE FROM audit_log WHERE id > $1", start)

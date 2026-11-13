from conftest import ADMIN, MEMBER

from app.permissions import Access, Perm
from app.resolve import Ids


def test_only_the_bots_own_permission_is_left(services):
    """Every other right is the supervisor's (docs/PLAN-front-end.md, *What stays, what goes*)."""
    assert [p.value for p in Perm] == ["bot_admin"]
    assert Access(services.ids, [ADMIN]).has(Perm.BOT_ADMIN)
    assert not Access(services.ids, [MEMBER]).has(Perm.BOT_ADMIN)


def test_before_the_server_is_known_nobody_holds_anything():
    assert not Access(Ids(), [ADMIN, MEMBER]).has(Perm.BOT_ADMIN)

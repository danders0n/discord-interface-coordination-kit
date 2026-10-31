from conftest import ADMIN, KADET, MEMBER, MISSION_MAKER, MODERATOR

from app.permissions import Access, Perm
from app.resolve import Ids


def test_a_member_creates_but_needs_approval(services):
    member = Access(services.ids, [MEMBER])
    assert member.may_create_events and member.may_sign_up
    assert not member.publishes_without_approval and not member.is_administration


def test_whoever_presses_publish_decides(services):
    assert Access(services.ids, [MEMBER, MISSION_MAKER]).publishes_without_approval
    assert Access(services.ids, [MEMBER, MODERATOR]).publishes_without_approval  # a moderator just publishes


def test_sign_up_roles_from_settings(services):
    assert Access(services.ids, [KADET]).may_sign_up
    assert not Access(services.ids, [KADET]).may_create_events
    assert not Access(services.ids, []).may_sign_up


def test_permissions_by_role(services):
    admin = Access(services.ids, [ADMIN])
    assert admin.has(Perm.BOT_ADMIN) and admin.has(Perm.MANAGE_ANY_EVENT) and admin.is_administration
    assert not admin.has(Perm.SERVER_CONTROL)


def test_before_the_server_is_known_nobody_holds_anything():
    nobody = Access(Ids(), [ADMIN, MEMBER, MISSION_MAKER])
    assert not nobody.has(Perm.BOT_ADMIN) and not nobody.is_member and not nobody.is_mission_maker

"""Other groups (docs/PLAN.md, *Event kinds*): the wizard and the panel suggest the known ones, and a new one is
added on the spot. *Manage any event* roles correct a group's name or acronym; a group is never deleted, because
events refer to it. Acronyms are kept in capitals and are unique."""

from __future__ import annotations

from sqlalchemy import func, select

from app.db import Database
from app.db.models import OtherGroup

MAX_ACRONYM = 10


class GroupError(ValueError):
    pass


def _acronym(text: str) -> str:
    acronym = text.strip().upper()
    if not 1 <= len(acronym) <= MAX_ACRONYM or not acronym.replace("-", "").isalnum():
        raise GroupError("acronym")
    return acronym


def _name(text: str) -> str:
    name = " ".join(text.split())
    if not 1 <= len(name) <= 100:
        raise GroupError("name")
    return name


class Groups:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def all(self) -> list[OtherGroup]:
        async with self.db.session() as session:
            return list((await session.execute(select(OtherGroup).order_by(OtherGroup.acronym))).scalars())

    async def get(self, group_id: int) -> OtherGroup | None:
        async with self.db.session() as session:
            return await session.get(OtherGroup, group_id)

    async def search(self, text: str, limit: int = 25) -> list[OtherGroup]:
        """For autocomplete: acronym or name containing ``text``."""
        like = f"%{text.strip()}%"
        async with self.db.session() as session:
            return list((await session.execute(select(OtherGroup).where(
                OtherGroup.acronym.ilike(like) | OtherGroup.name.ilike(like)).order_by(OtherGroup.acronym)
                .limit(limit))).scalars())

    async def add(self, acronym: str, name: str, member_id: int) -> OtherGroup:
        """The group with this acronym, added now if it is new."""
        acronym, name = _acronym(acronym), _name(name)
        async with self.db.session() as session, session.begin():
            found = (await session.execute(select(OtherGroup).where(
                func.upper(OtherGroup.acronym) == acronym))).scalar_one_or_none()
            if found is None:
                found = OtherGroup(acronym=acronym, name=name, added_by=member_id)
                session.add(found)
        return found

    async def correct(self, group_id: int, acronym: str, name: str) -> OtherGroup:
        acronym, name = _acronym(acronym), _name(name)
        async with self.db.session() as session, session.begin():
            group = await session.get(OtherGroup, group_id)
            if group is None:
                raise GroupError("not_found")
            clash = (await session.execute(select(OtherGroup).where(
                func.upper(OtherGroup.acronym) == acronym, OtherGroup.id != group_id))).scalar_one_or_none()
            if clash is not None:
                raise GroupError("acronym_taken")
            group.acronym, group.name = acronym, name
        return group

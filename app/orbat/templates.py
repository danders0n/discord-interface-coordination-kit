"""Shared ORBAT templates (docs/PLAN.md, *ORBAT and slots*, *Templates*): every event creator uses them; the member
who saved one and *manage any event* roles rename, overwrite or delete it. An event copies a template's text, so a
later change of the template doesn't touch it. Refusals are :class:`TemplateError` codes the Discord layer words."""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import Database
from app.db.models import OrbatTemplate

MAX_NAME = 100


class Problem(StrEnum):
    NOT_FOUND = "not_found"
    NOT_ALLOWED = "not_allowed"
    NAME_TAKEN = "name_taken"
    BAD_NAME = "bad_name"


class TemplateError(Exception):
    def __init__(self, problem: Problem) -> None:
        super().__init__(problem.value)
        self.problem = problem


def _clean(name: str) -> str:
    name = " ".join(name.split())
    if not 1 <= len(name) <= MAX_NAME:
        raise TemplateError(Problem.BAD_NAME)
    return name


class Templates:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def all(self) -> list[OrbatTemplate]:
        async with self.db.session() as session:
            return list((await session.execute(select(OrbatTemplate).order_by(OrbatTemplate.name))).scalars())

    async def get(self, template_id: int) -> OrbatTemplate:
        async with self.db.session() as session:
            found = await session.get(OrbatTemplate, template_id)
        if found is None:
            raise TemplateError(Problem.NOT_FOUND)
        return found

    async def save(self, name: str, text: str, member_id: int, manage_any: bool) -> OrbatTemplate:
        """Save as a new template, or overwrite one of the same name the member may change."""
        name = _clean(name)
        async with self.db.session() as session, session.begin():
            found = (await session.execute(select(OrbatTemplate).where(OrbatTemplate.name == name))).scalar_one_or_none()
            if found is None:
                found = OrbatTemplate(name=name, owner_id=member_id, text=text)
                session.add(found)
            elif found.owner_id == member_id or manage_any:
                found.text = text
            else:
                raise TemplateError(Problem.NAME_TAKEN)
        return found

    async def rename(self, template_id: int, name: str, member_id: int, manage_any: bool) -> OrbatTemplate:
        name = _clean(name)
        try:
            async with self.db.session() as session, session.begin():
                found = await self._mine(session, template_id, member_id, manage_any)
                found.name = name
        except IntegrityError as exc:
            raise TemplateError(Problem.NAME_TAKEN) from exc
        return found

    async def delete(self, template_id: int, member_id: int, manage_any: bool) -> None:
        async with self.db.session() as session, session.begin():
            await session.delete(await self._mine(session, template_id, member_id, manage_any))

    @staticmethod
    async def _mine(session, template_id: int, member_id: int, manage_any: bool) -> OrbatTemplate:
        found = await session.get(OrbatTemplate, template_id)
        if found is None:
            raise TemplateError(Problem.NOT_FOUND)
        if found.owner_id != member_id and not manage_any:
            raise TemplateError(Problem.NOT_ALLOWED)
        return found

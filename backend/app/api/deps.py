"""Request-scoped dependencies shared by the registry endpoints.

One place for the session dependency and the pagination bounds, because both are
cross-cutting and both are easy to get subtly different per module — and a limit
that is 200 in one handler and unbounded in the next is a limit that does not
exist as far as the corpus is concerned.

**The session is yielded, not committed here.** `get_session` opens a
transaction; whether a request commits or rolls back is the handler's decision,
because the registry endpoints do not all want the same thing. A read-only GET
that commits has written nothing and paid for a round trip; a handler that
raises after a partial write needs the rollback. Committing here would take that
choice away from every route.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session

#: Matches `components/parameters/Limit` in contracts/openapi.yaml.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


async def db_session() -> AsyncIterator[AsyncSession]:
    """The request-scoped session.

    A thin re-export of `get_session` rather than a second implementation: two
    session factories would be two transaction scopes, and which one a handler
    used would depend on how it happened to be written.

    The annotations are resolved by FastAPI at import time, so `AsyncSession` is
    imported at runtime here — see the note in `app/core/ratelimit.py` for why a
    `TYPE_CHECKING` import would break this silently.
    """
    async for session in get_session():
        yield session


#: Injected session. `Annotated` rather than a default value so a handler cannot
#: accidentally be written without it.
Session = Annotated[AsyncSession, Depends(db_session)]

#: Offset pagination, bounded by the contract. `limit` and `offset` are
#: `Query` parameters rather than a pagination model because FastAPI's generated
#: OpenAPI then carries the same bounds the contract declares, and the two cannot
#: disagree.
Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Offset = Annotated[int, Query(ge=0)]


__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "Limit", "Offset", "Session", "db_session"]

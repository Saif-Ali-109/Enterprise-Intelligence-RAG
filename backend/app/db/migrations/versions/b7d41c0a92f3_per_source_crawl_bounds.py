"""per-source crawl bounds

Revision ID: b7d41c0a92f3
Revises: eca01b2db9db
Create Date: 2026-10-03 09:41:07.119204

Three nullable columns on `sources`, plus their range constraints.

Why a migration rather than crawl-request arguments: `SourceUpdate` in
contracts/openapi.yaml accepts `max_pages`, `max_depth`, and `delay_seconds`,
so a client can change them on a registered source. A bound that is validated
and then discarded is worse than an API that never offered it — the client
believes the next crawl will be bounded and it is not. Null means "use the
configured default"; the orchestrator treats null as not-overridden rather
than as zero, because a null page cap that became zero would produce an empty
frontier rather than the configured one.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d41c0a92f3"
down_revision: str | None = "eca01b2db9db"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("max_pages", sa.Integer(), nullable=True))
    op.add_column("sources", sa.Column("max_depth", sa.Integer(), nullable=True))
    op.add_column("sources", sa.Column("delay_seconds", sa.Float(), nullable=True))

    op.create_check_constraint(
        "ck_sources_max_pages_range",
        "sources",
        "max_pages IS NULL OR max_pages BETWEEN 1 AND 500",
    )
    op.create_check_constraint(
        "ck_sources_max_depth_range",
        "sources",
        "max_depth IS NULL OR max_depth BETWEEN 0 AND 5",
    )
    op.create_check_constraint(
        "ck_sources_delay_seconds_range",
        "sources",
        "delay_seconds IS NULL OR delay_seconds BETWEEN 0.1 AND 60",
    )


def downgrade() -> None:
    op.drop_constraint("ck_sources_delay_seconds_range", "sources", type_="check")
    op.drop_constraint("ck_sources_max_depth_range", "sources", type_="check")
    op.drop_constraint("ck_sources_max_pages_range", "sources", type_="check")
    op.drop_column("sources", "delay_seconds")
    op.drop_column("sources", "max_depth")
    op.drop_column("sources", "max_pages")

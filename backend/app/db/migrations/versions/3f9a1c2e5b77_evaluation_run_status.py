"""evaluation_runs.status

Revision ID: 3f9a1c2e5b77
Revises: b7d41c0a92f3
Create Date: 2026-10-06 10:30:00.000000

The contract (`openapi.yaml`, EvaluationRun) has carried `status` since the
schema was written; the table never got the column. A run is asynchronous —
queued, then running, then completed or failed — and an endpoint polled on a
run id needs to distinguish "no metrics yet, still running" from "no metrics
yter.metrics is None.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3f9a1c2e5b77"
down_revision: str | None = "b7d41c0a92f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "evaluation_runs",
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
    )
    op.create_check_constraint(
        "ck_evaluation_runs_status_valid",
        "evaluation_runs",
        "status IN ('queued', 'running', 'completed', 'failed')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_evaluation_runs_status_valid", "evaluation_runs", type_="check")
    op.drop_column("evaluation_runs", "status")

"""add document kinds and generation flow support"""

from alembic import op
import sqlalchemy as sa


revision = "20260512_0002"
down_revision = "20260511_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="sources"),
    )
    op.create_index("ix_documents_kind", "documents", ["kind"], unique=False)
    op.drop_constraint("uq_documents_thread_stem", "documents", type_="unique")
    op.create_unique_constraint(
        "uq_documents_thread_kind_stem",
        "documents",
        ["thread_id", "kind", "stem"],
    )
    op.alter_column("documents", "kind", server_default=None)


def downgrade() -> None:
    op.drop_constraint("uq_documents_thread_kind_stem", "documents", type_="unique")
    op.create_unique_constraint("uq_documents_thread_stem", "documents", ["thread_id", "stem"])
    op.drop_index("ix_documents_kind", table_name="documents")
    op.drop_column("documents", "kind")

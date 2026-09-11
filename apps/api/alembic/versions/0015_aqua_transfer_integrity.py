"""Aquaculture transfer identity, paired roles, and explicit destination batches.

Legacy source-only TRANSFER events remain nullable because their destination
batch cannot be reconstructed safely from destination_unit_id alone.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0015_aqua_transfer_integrity"
down_revision: str | None = "0014_password_recovery"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE TYPE production_transfer_role AS ENUM ('out', 'in')")
    role_type = postgresql.ENUM("out", "in", name="production_transfer_role", create_type=False)
    if bind.dialect.name != "postgresql":
        role_type = sa.String(3)

    op.create_table(
        "production_transfers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("farm_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("destination_batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_unit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("destination_unit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("transfer_loss", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("payload_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_batch_id <> destination_batch_id", name="ck_prod_transfer_batches_distinct"
        ),
        sa.CheckConstraint(
            "source_unit_id <> destination_unit_id", name="ck_prod_transfer_units_distinct"
        ),
        sa.CheckConstraint("quantity > 0", name="ck_prod_transfer_quantity_positive"),
        sa.CheckConstraint("transfer_loss >= 0", name="ck_prod_transfer_loss_nonnegative"),
        sa.ForeignKeyConstraint(["source_batch_id"], ["production_batches.id"]),
        sa.ForeignKeyConstraint(["destination_batch_id"], ["production_batches.id"]),
        sa.ForeignKeyConstraint(["source_unit_id"], ["production_units.id"]),
        sa.ForeignKeyConstraint(["destination_unit_id"], ["production_units.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_production_transfer_source_idempotency",
        "production_transfers",
        ["source_batch_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.add_column(
        "production_events", sa.Column("transfer_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column("production_events", sa.Column("transfer_role", role_type, nullable=True))
    op.create_foreign_key(
        "fk_production_events_transfer_id",
        "production_events",
        "production_transfers",
        ["transfer_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_production_events_transfer_id", "production_events", ["transfer_id"])
    op.create_index(
        "uq_production_event_transfer_role",
        "production_events",
        ["transfer_id", "transfer_role"],
        unique=True,
        postgresql_where=sa.text("transfer_id IS NOT NULL"),
        sqlite_where=sa.text("transfer_id IS NOT NULL"),
    )
    if bind.dialect.name == "postgresql":
        op.execute(
            """
        CREATE FUNCTION production_transfer_topology_validate() RETURNS TRIGGER AS $$
        DECLARE topology_matches INT;
        BEGIN
          SELECT COUNT(*) INTO topology_matches
            FROM production_batches source_batch
            JOIN production_units source_unit ON source_unit.id = source_batch.unit_id
            JOIN production_sites source_site ON source_site.id = source_unit.site_id
            JOIN farms source_farm ON source_farm.id = source_site.farm_id
            JOIN production_batches destination_batch
              ON destination_batch.id = NEW.destination_batch_id
            JOIN production_units destination_unit
              ON destination_unit.id = destination_batch.unit_id
            JOIN production_sites destination_site
              ON destination_site.id = destination_unit.site_id
            JOIN farms destination_farm ON destination_farm.id = destination_site.farm_id
           WHERE source_batch.id = NEW.source_batch_id
             AND source_unit.id = NEW.source_unit_id
             AND destination_unit.id = NEW.destination_unit_id
             AND source_farm.id = NEW.farm_id
             AND destination_farm.id = NEW.farm_id
             AND source_farm.organization_id = NEW.organization_id
             AND destination_farm.organization_id = NEW.organization_id;
          IF topology_matches <> 1 THEN
            RAISE EXCEPTION 'production transfer does not match authoritative organization and farm topology'
              USING ERRCODE='integrity_constraint_violation';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql
        """
        )
        op.execute(
            """
        CREATE TRIGGER trg_production_transfer_topology_validate
        BEFORE INSERT OR UPDATE ON production_transfers FOR EACH ROW
        EXECUTE FUNCTION production_transfer_topology_validate()
        """
        )
        op.execute(
            """
        CREATE FUNCTION production_transfer_identity_immutable() RETURNS TRIGGER AS $$
        BEGIN
          IF ROW(OLD.id, OLD.organization_id, OLD.farm_id, OLD.source_batch_id,
                 OLD.destination_batch_id, OLD.source_unit_id, OLD.destination_unit_id,
                 OLD.quantity, OLD.transfer_loss)
             IS DISTINCT FROM
             ROW(NEW.id, NEW.organization_id, NEW.farm_id, NEW.source_batch_id,
                 NEW.destination_batch_id, NEW.source_unit_id, NEW.destination_unit_id,
                 NEW.quantity, NEW.transfer_loss) THEN
            RAISE EXCEPTION 'production transfer identity and topology are immutable'
              USING ERRCODE='integrity_constraint_violation';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql
        """
        )
        op.execute(
            """
        CREATE TRIGGER trg_production_transfer_identity_immutable
        BEFORE UPDATE ON production_transfers FOR EACH ROW
        EXECUTE FUNCTION production_transfer_identity_immutable()
        """
        )
        op.execute(
            """
        CREATE FUNCTION production_transfer_event_topology_immutable() RETURNS TRIGGER AS $$
        DECLARE expected_org UUID; expected_farm UUID; expected_batch UUID; expected_unit UUID;
                expected_site UUID; expected_quantity INT; expected_loss INT;
        BEGIN
          IF TG_OP = 'UPDATE' AND OLD.created_at IS DISTINCT FROM NEW.created_at THEN
            RAISE EXCEPTION 'production event creation chronology is immutable'
              USING ERRCODE='integrity_constraint_violation';
          END IF;
          IF TG_OP = 'UPDATE' AND (OLD.transfer_id IS DISTINCT FROM NEW.transfer_id
             OR OLD.transfer_role IS DISTINCT FROM NEW.transfer_role) THEN
            RAISE EXCEPTION 'production transfer event identity and role are immutable'
              USING ERRCODE='integrity_constraint_violation';
          END IF;
          IF (NEW.transfer_id IS NULL) <> (NEW.transfer_role IS NULL) THEN
            RAISE EXCEPTION 'production transfer id and role must be set together'
              USING ERRCODE='integrity_constraint_violation';
          END IF;
          IF NEW.event_type = 'TRANSFER'
             AND NEW.transfer_id IS NULL AND NEW.transfer_role IS NULL THEN
            IF TG_OP = 'INSERT' THEN
              RAISE EXCEPTION 'new production transfers require normalized topology'
                USING ERRCODE='integrity_constraint_violation';
            ELSIF OLD.event_type <> 'TRANSFER' OR OLD.transfer_id IS NOT NULL
               OR OLD.transfer_role IS NOT NULL THEN
              RAISE EXCEPTION 'new production transfers require normalized topology'
                USING ERRCODE='integrity_constraint_violation';
            END IF;
          END IF;
          IF NEW.transfer_id IS NOT NULL THEN
            IF NEW.event_type <> 'TRANSFER' THEN
              RAISE EXCEPTION 'production transfer topology is only valid on TRANSFER events'
                USING ERRCODE='integrity_constraint_violation';
            END IF;
            SELECT transfer.organization_id, transfer.farm_id,
                   CASE NEW.transfer_role WHEN 'out' THEN source_batch_id ELSE destination_batch_id END,
                   CASE NEW.transfer_role WHEN 'out' THEN source_unit_id ELSE destination_unit_id END,
                   site.id, transfer.quantity, transfer.transfer_loss
              INTO expected_org, expected_farm, expected_batch, expected_unit,
                   expected_site, expected_quantity, expected_loss
              FROM production_transfers transfer
              JOIN production_units unit ON unit.id = CASE NEW.transfer_role
                WHEN 'out' THEN transfer.source_unit_id ELSE transfer.destination_unit_id END
              JOIN production_sites site ON site.id = unit.site_id
             WHERE transfer.id=NEW.transfer_id;
            IF expected_batch IS NULL OR NEW.organization_id <> expected_org
               OR NEW.farm_id <> expected_farm OR NEW.batch_id <> expected_batch
               OR NEW.unit_id <> expected_unit OR NEW.site_id <> expected_site THEN
              RAISE EXCEPTION 'production transfer event role does not match transfer topology'
                USING ERRCODE='integrity_constraint_violation';
            END IF;
            IF jsonb_typeof(NEW.data -> 'quantity') <> 'number'
               OR (NEW.data ->> 'quantity')::numeric <> expected_quantity
               OR jsonb_typeof(NEW.data -> 'transfer_loss') <> 'number'
               OR (NEW.data ->> 'transfer_loss')::numeric <> expected_loss THEN
              RAISE EXCEPTION 'production transfer event payload does not match normalized transfer'
                USING ERRCODE='integrity_constraint_violation';
            END IF;
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql
        """
        )
        op.execute(
            """
        CREATE TRIGGER trg_production_transfer_event_topology_immutable
        BEFORE INSERT OR UPDATE ON production_events FOR EACH ROW
        EXECUTE FUNCTION production_transfer_event_topology_immutable()
        """
        )
        op.execute(
            """
        CREATE FUNCTION production_transfer_pair_complete() RETURNS TRIGGER AS $$
        DECLARE target UUID; outs INT; ins INT;
        BEGIN
          IF TG_TABLE_NAME = 'production_transfers' THEN target := NEW.id;
          ELSIF TG_OP = 'DELETE' THEN target := OLD.transfer_id;
          ELSE target := NEW.transfer_id; END IF;
          IF target IS NULL THEN RETURN NULL; END IF;
          SELECT COUNT(*) FILTER (WHERE transfer_role='out'), COUNT(*) FILTER (WHERE transfer_role='in')
            INTO outs, ins FROM production_events WHERE transfer_id=target;
          IF outs <> 1 OR ins <> 1 THEN
            RAISE EXCEPTION 'production transfer % must have exactly one OUT and one IN', target
              USING ERRCODE='integrity_constraint_violation';
          END IF;
          RETURN NULL;
        END; $$ LANGUAGE plpgsql
        """
        )
        op.execute(
            """
        CREATE CONSTRAINT TRIGGER trg_production_transfer_pair_complete
        AFTER INSERT OR UPDATE OR DELETE ON production_events
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION production_transfer_pair_complete()
        """
        )
        op.execute(
            """
        CREATE CONSTRAINT TRIGGER trg_production_transfer_row_pair_complete
        AFTER INSERT OR UPDATE ON production_transfers
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION production_transfer_pair_complete()
        """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_production_transfer_row_pair_complete ON production_transfers"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_production_transfer_pair_complete ON production_events"
        )
        op.execute("DROP FUNCTION IF EXISTS production_transfer_pair_complete()")
        op.execute(
            "DROP TRIGGER IF EXISTS trg_production_transfer_event_topology_immutable ON production_events"
        )
        op.execute("DROP FUNCTION IF EXISTS production_transfer_event_topology_immutable()")
        op.execute(
            "DROP TRIGGER IF EXISTS trg_production_transfer_identity_immutable ON production_transfers"
        )
        op.execute("DROP FUNCTION IF EXISTS production_transfer_identity_immutable()")
        op.execute(
            "DROP TRIGGER IF EXISTS trg_production_transfer_topology_validate ON production_transfers"
        )
        op.execute("DROP FUNCTION IF EXISTS production_transfer_topology_validate()")
    op.drop_index("uq_production_event_transfer_role", table_name="production_events")
    op.drop_index("ix_production_events_transfer_id", table_name="production_events")
    op.drop_constraint("fk_production_events_transfer_id", "production_events", type_="foreignkey")
    op.drop_column("production_events", "transfer_role")
    op.drop_column("production_events", "transfer_id")
    op.drop_index("uq_production_transfer_source_idempotency", table_name="production_transfers")
    op.drop_table("production_transfers")
    if bind.dialect.name == "postgresql":
        op.execute("DROP TYPE production_transfer_role")

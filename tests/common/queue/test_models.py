from pgqueuer.domain.settings import DBSettings

from aizk.common.queue import QueueSchema


def test_queue_schema_reads_pgqueuer_names_and_derives_sequences() -> None:
    settings = DBSettings()
    schema = QueueSchema.from_settings(settings)

    assert schema.tables == (
        settings.queue_table,
        settings.queue_table_log,
        settings.statistics_table,
        settings.schedules_table,
    )
    assert schema.sequences == tuple(f"{table}_id_seq" for table in schema.tables)
    assert schema.status_type == settings.queue_status_type

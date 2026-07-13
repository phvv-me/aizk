-- The watermark_kind enum was generated without entity_dirty, the value the on-write
-- profile chain bumps per touched entity; a fresh alembic database carries it.
ALTER TYPE watermark_kind ADD VALUE IF NOT EXISTS 'entity_dirty' BEFORE 'fact_count';

import uuid
from datetime import datetime

from sqlmodel import Field

from .fields import tz_datetime_field


class Id:
    """A client-generated uuid primary key, the one surrogate-id strategy every table but the
    identity join tables shares.

    uuid7 over uuid4: its leading bits carry a millisecond timestamp, so ids generated close in
    time sort close together, keeping new rows landing at one edge of the primary key's b-tree
    instead of a uuid4's fully random insert point scattering writes across the whole index.

    id: stable identity, generated client-side on insert unless the caller passes its own
        content-addressed uuid5, as entities and facts do.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)


class Timestamped:
    """A first-seen `created_at` server-stamped on insert.

    created_at: first-seen timestamp.
    """

    created_at: datetime = tz_datetime_field()

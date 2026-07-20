from typing import Annotated, Literal

from patos import FrozenModel
from pydantic import Field, StringConstraints

from ..artifacts.models import ArtifactReceipt
from ..artifacts.uploads import Sha256Hex
from ..memory import WriteResult


class UploadDeclaration(FrozenModel):
    """One local file whose exact bytes a client intends to preserve."""

    filename: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
    ]
    media_type: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
    ]
    size: Annotated[int, Field(gt=0)]
    sha256: Sha256Hex


class UploadTicketAccepted(FrozenModel):
    """One short-lived private URL that accepts the declared bytes exactly once."""

    status: Literal["accepted"] = "accepted"
    upload_url: str
    expires_seconds: Annotated[int, Field(gt=0)]


type RememberResult = WriteResult | ArtifactReceipt | UploadTicketAccepted

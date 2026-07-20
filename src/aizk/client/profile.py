import json
import os
from pathlib import Path

from .models import ClientProfile


class ProfileStore:
    """Persist nonsecret client connection preferences under the XDG config root."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or self.default_path()

    @staticmethod
    def default_path() -> Path:
        """Return the configured XDG path or the conventional user fallback."""
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return root / "aizk" / "profile.json"

    def load(self) -> ClientProfile:
        """Load the selected server or fail with a direct login instruction."""
        try:
            serialized = self.path.read_text(encoding="utf-8")
        except FileNotFoundError as missing:
            raise FileNotFoundError(
                f"no AIZK client profile at {self.path}, run `aizk auth login --server URL`"
            ) from missing
        return ClientProfile.model_validate_json(serialized)

    def save(self, profile: ClientProfile) -> Path:
        """Atomically save only nonsecret connection settings."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                profile.model_dump(mode="json"),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(self.path)
        return self.path

from pydantic_settings import BaseSettings, SettingsConfigDict


class EvaluationSettings(BaseSettings):
    """Configuration used only by the standalone evaluation process."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="AIZK_EVAL_",
        extra="ignore",
    )

    api_key: str = ""
    concurrency: int = 4
    judge: bool = False
    judge_model: str = ""
    max_tokens: int = 512
    model: str = ""
    url: str = ""


settings = EvaluationSettings()

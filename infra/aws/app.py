from aws_cdk import App

from .config import DeploymentConfig
from .stack import AizkAwsStack


def synthesize() -> None:
    """Synthesize one bootstrap or runtime stack from non-secret environment inputs."""
    app = App()
    config = DeploymentConfig.from_environment()
    AizkAwsStack(app, "AizkCockroachdb", config)
    app.synth()


if __name__ == "__main__":
    synthesize()

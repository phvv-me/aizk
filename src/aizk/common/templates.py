from jinja2 import Environment, PackageLoader


def markdown_environment(package: str) -> Environment:
    """Async Jinja environment over one package's `templates/` directory.

    package: dotted package name that owns the templates it renders.
    """
    return Environment(
        loader=PackageLoader(package, "templates"),
        enable_async=True,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
    )

from collections.abc import Awaitable, Callable

# the client verbs the server registers, the whole surface a key-holder reaches; every operational
# operation now lives in the CLI rather than as a tagged, listing-hidden tool.
USER_TOOLS = {
    "recall",
    "remember",
    "reference",
    "move",
}


def text_of(result: object) -> str:
    """The rendered string a str-returning tool carries on its structured content.

    result: the `ToolResult` a `tool.run` resolved to.
    """
    content = getattr(result, "structured_content", None)
    assert isinstance(content, dict)
    return content["result"]


def const[T](value: T) -> Callable[..., Awaitable[T]]:
    """An async function ignoring its arguments and resolving to `value`, a seam stand-in.

    value: the constant the returned coroutine yields.
    """

    async def fixed(*args: object, **kwargs: object) -> T:
        return value

    return fixed


class Rendered:
    """A report stand-in whose `render()` is the one text-producing seam a tool body calls.

    text: the fixed string `render()` returns, so a test asserts on the tool's output directly.
    """

    def __init__(self, text: str) -> None:
        self.text = text

    def render(self) -> str:
        return self.text

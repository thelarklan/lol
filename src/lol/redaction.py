from __future__ import annotations


class StreamingRedactor:
    def __init__(self, secrets: list[str] | tuple[str, ...]) -> None:
        self._secrets = sorted(
            {item for item in secrets if item}, key=lambda item: (-len(item), item)
        )
        self._tail = ""
        self._max_length = max((len(item) for item in self._secrets), default=1)

    def _consume(self, text: str, *, final: bool) -> tuple[str, str]:
        boundary = len(text) if final else max(0, len(text) - self._max_length + 1)
        output: list[str] = []
        position = 0
        while position < boundary:
            secret = next(
                (item for item in self._secrets if text.startswith(item, position)),
                None,
            )
            if secret is None:
                output.append(text[position])
                position += 1
            else:
                output.append("****")
                position += len(secret)
        return "".join(output), text[position:]

    def feed(self, text: str) -> str:
        output, self._tail = self._consume(self._tail + text, final=False)
        return output

    def finish(self) -> str:
        output, self._tail = self._consume(self._tail, final=True)
        return output

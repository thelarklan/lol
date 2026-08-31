from __future__ import annotations


class StreamingRedactor:
    def __init__(self, secrets: list[str] | tuple[str, ...]) -> None:
        self._secrets = sorted({item for item in secrets if item}, key=len, reverse=True)
        self._tail = ""
        self._overlap = max((len(item) for item in self._secrets), default=1) - 1

    def _replace(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "****")
        return text

    def feed(self, text: str) -> str:
        combined = self._tail + text
        if self._overlap <= 0:
            self._tail = ""
            return self._replace(combined)
        if len(combined) <= self._overlap:
            self._tail = combined
            return ""
        boundary = len(combined) - self._overlap
        for secret in self._secrets:
            start = combined.rfind(secret, 0, boundary + len(secret))
            if start != -1 and start < boundary < start + len(secret):
                boundary = start
        output, self._tail = combined[:boundary], combined[boundary:]
        return self._replace(output)

    def finish(self) -> str:
        output = self._replace(self._tail)
        self._tail = ""
        return output

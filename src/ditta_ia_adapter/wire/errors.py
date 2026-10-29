"""The rejection raised by every layer of the wire package."""


class Rejected(ValueError):
    """A message that must be rejected.

    `code` is one of the conformance rejection codes from the spec's
    Conformance section (for example `missing_field` or `bad_hex`). Those
    name the violated rule; they are not wire error codes. The front maps
    them to an Error message, almost always `malformed`.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail

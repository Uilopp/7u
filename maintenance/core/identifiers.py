"""Exact message identifiers crossing model interfaces must never be coerced."""


def message_id(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 19
        or not value.isascii()
        or not value.isdigit()
        or not 0 < int(value) <= 2**63 - 1
    ):
        raise ValueError("msg_id must be a positive signed-64-bit decimal string")
    return value

"""Shared message semantics; preserve original text and distinguish client-generated placeholders."""

PLACEHOLDERS = frozenset(
    {
        "[闪照]请使用新版手机QQ查看闪照。",
        "该条消息暂不支持查看",
    }
)


def is_authored_text(text):
    return isinstance(text, str) and bool(text.strip()) and text not in PLACEHOLDERS

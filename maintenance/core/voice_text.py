"""One policy for voice text admitted to product-facing text consumers."""

USABLE_VOICE_STATES = frozenset({"verified", "asr_accepted"})
USABLE_VOICE_STATES_SQL = "('verified','asr_accepted')"


def usable_voice_text(row):
    return row.get("normalized_kind") != "voice" or row.get("voice_state") in USABLE_VOICE_STATES

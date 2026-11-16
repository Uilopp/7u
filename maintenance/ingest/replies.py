"""QQNT reply elements: quoted metadata is separate from authored text."""


def extract_reply(contents):
    references = []
    authored = []
    other_types = []
    for element in contents:
        if element.content_type == 7:
            references.append(
                {
                    "msg_id": str(element.reply_msg_id) if element.reply_msg_id else None,
                    "sequence": str(element.reply_msg_seq) if element.reply_msg_seq else None,
                    "sender_uid": element.sender_uid or None,
                    "timestamp": element.reply_msg_time_alt or None,
                    "summary": element.reply_summary or None,
                }
            )
        elif element.content_type == 1:
            if element.text:
                authored.append(element.text)
        else:
            other_types.append(element.content_type)
    return {
        "text": "\n".join(authored) if authored else None,
        "references": references,
        "other_element_types": other_types,
    }

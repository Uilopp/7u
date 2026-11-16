import unittest
from types import SimpleNamespace

from maintenance.ingest.replies import extract_reply


class ReplyTests(unittest.TestCase):
    def test_reference_never_becomes_author_text_and_large_ids_stay_exact(self):
        ref = SimpleNamespace(
            content_type=7,
            reply_msg_id=7512497993392950692,
            reply_msg_seq=58837,
            sender_uid="quoted-speaker",
            reply_msg_time_alt=1749139730,
            reply_summary="引用的内容",
            text="不能当正文",
        )
        text = SimpleNamespace(content_type=1, text="真正的回复")
        image = SimpleNamespace(content_type=2, text="图片描述不是正文")
        result = extract_reply([ref, text, image, SimpleNamespace(content_type=1, text="第二段")])
        self.assertEqual(result["text"], "真正的回复\n第二段")
        self.assertEqual(result["references"][0]["msg_id"], "7512497993392950692")
        self.assertEqual(result["other_element_types"], [2])

    def test_no_authored_text_is_not_filled_from_summary(self):
        ref = SimpleNamespace(
            content_type=7,
            reply_msg_id=0,
            reply_msg_seq=0,
            sender_uid="",
            reply_msg_time_alt=0,
            reply_summary="quoted",
        )
        result = extract_reply([ref])
        self.assertIsNone(result["text"])
        self.assertIsNone(result["references"][0]["msg_id"])

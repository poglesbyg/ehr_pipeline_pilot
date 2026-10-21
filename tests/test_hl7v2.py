import unittest

from hl7bridge.hl7v2 import Delimiters, HL7ParseError, Message, build_ack

ADT = (
    "MSH|^~\\&|VISTA ADT|999^PILOT^DNS|HL7BRIDGE|NAII|20261020081500-0500||ADT^A01^ADT_A01|C1|P|2.4\r"
    "PID|1||111V1^^^USVHA&&0363^NI~7171^^^USVHA&&0363^PI^VA FACILITY ID&999&L||DOE^JANE\\T\\ANN^Q||19600101|F\r"
    "NTE|1||Line one\\.br\\Pipe \\F\\ caret \\S\\ amp \\T\\ tilde \\R\\ esc \\E\\ hex \\X41\\"
)


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.msg = Message.parse(ADT)

    def test_header_shortcuts(self):
        self.assertEqual(self.msg.type_label, "ADT^A01")
        self.assertEqual(self.msg.control_id, "C1")
        self.assertEqual(self.msg.version, "2.4")
        self.assertEqual(self.msg.get("MSH-1"), "|")
        self.assertEqual(self.msg.get("MSH-2"), "^~\\&")

    def test_paths_repetitions_subcomponents(self):
        self.assertEqual(self.msg.get("PID-3.1"), "111V1")
        self.assertEqual(self.msg.get("PID-3(2).1"), "7171")
        self.assertEqual(self.msg.get("PID-3(2).6.2"), "999")
        self.assertEqual(self.msg.get("PID-3.4.3"), "0363")
        self.assertEqual(self.msg.get("PID-99"), "")
        self.assertEqual(self.msg.get("ZZZ-1"), "")

    def test_escape_sequences_decoded_after_split(self):
        self.assertEqual(self.msg.get("PID-5.2"), "JANE&ANN")
        self.assertEqual(self.msg.get("NTE-3"),
                         "Line one\nPipe | caret ^ amp & tilde ~ esc \\ hex A")

    def test_round_trip(self):
        again = Message.parse(self.msg.to_er7())
        self.assertEqual(again.to_er7(), self.msg.to_er7())
        self.assertEqual(again.get("NTE-3"), self.msg.get("NTE-3"))

    def test_custom_delimiters(self):
        msg = Message.parse("MSH#*!/$#APP#FAC####ADT*A08#X9#P#2.5\nPID#1##A1*B$C!A2")
        self.assertEqual(msg.delimiters, Delimiters("#", "*", "!", "/", "$"))
        self.assertEqual(msg.get("PID-3(1).2.2"), "C")
        self.assertEqual(msg.get("PID-3(2).1"), "A2")

    def test_mllp_framing_and_line_endings_tolerated(self):
        msg = Message.parse("\x0b" + ADT.replace("\r", "\r\n") + "\x1c\r")
        self.assertEqual(len(msg.segments), 3)

    def test_invalid_input(self):
        for bad in ("", "PID|1", "MSH|^^~\\&|x", "MSH|^~\\&|A\rpid|1"):
            with self.assertRaises(HL7ParseError, msg=bad):
                Message.parse(bad)

    def test_groups(self):
        msg = Message.parse("MSH|^~\\&|A|B|||||ORU^R01|1|P|2.5\rPID|1\rOBR|1\rOBX|1\rNTE|1\rOBX|2\rOBR|2\rOBX|1")
        groups = [(h.get(1), [c.name for c in kids]) for h, kids in msg.groups("OBR", {"OBX", "NTE"})]
        self.assertEqual(groups, [("1", ["OBX", "NTE", "OBX"]), ("2", ["OBX"])])


class AckTests(unittest.TestCase):
    def test_ack_swaps_endpoints_and_echoes_control_id(self):
        ack = build_ack(Message.parse(ADT), "AA", "ok")
        self.assertEqual(ack.get("MSH-3"), "HL7BRIDGE")
        self.assertEqual(ack.get("MSH-5"), "VISTA ADT")
        self.assertEqual(ack.get("MSH-6.1"), "999")
        self.assertEqual(ack.get("MSH-9"), "ACK")
        self.assertEqual(ack.get("MSH-9.2"), "A01")
        self.assertEqual(ack.get("MSA-1"), "AA")
        self.assertEqual(ack.get("MSA-2"), "C1")
        self.assertIsNone(ack.segment("ERR"))

    def test_error_ack_has_err_segment_and_escapes_text(self):
        ack = build_ack(Message.parse(ADT), "AE", "bad | value", "101")
        self.assertEqual(ack.get("ERR-3.1"), "101")
        self.assertEqual(ack.get("ERR-3.3"), "HL70357")
        self.assertEqual(ack.get("ERR-8"), "bad | value")

    def test_reject_without_parseable_original(self):
        ack = build_ack(None, "AR", "garbage", "100")
        self.assertEqual(ack.get("MSA-1"), "AR")


if __name__ == "__main__":
    unittest.main()

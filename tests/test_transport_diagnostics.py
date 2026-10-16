from __future__ import annotations

import unittest
from types import SimpleNamespace

from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close, CloseCode

from live_subtitle_translator.transport_diagnostics import (
    describe_transport_error,
    is_expected_session_rotation,
)


SECRET = "secret-key https://private.example/audio?key=secret subtitle: private words"


class TransportDiagnosticsTests(unittest.TestCase):
    def test_abnormal_close_keeps_os_error_codes_without_messages(self):
        cause = ConnectionResetError(10054, SECRET)
        cause.winerror = 10054
        error = ConnectionClosedError(None, None)
        error.__cause__ = cause
        result = describe_transport_error(error)
        self.assertEqual(result,
            "chain=ConnectionClosedError[received_close=none,sent_close=none,abnormal_eof=true]"
            " <- ConnectionResetError[errno=10054,winerror=10054]")
        self.assertNotIn(SECRET, result)

    def test_eof_context_is_included(self):
        error = ConnectionClosedError(None, None)
        error.__context__ = EOFError(SECRET)
        self.assertIn(" <- EOFError[abnormal_eof=true]", describe_transport_error(error))

    def test_explicit_close_codes_do_not_expose_close_reasons(self):
        error = ConnectionClosedError(
            Close(CloseCode.INTERNAL_ERROR, SECRET), Close(1000, SECRET), True)
        self.assertEqual(describe_transport_error(error),
            "chain=ConnectionClosedError[received_close=1011,sent_close=1000,abnormal_eof=false]")

    def test_sent_close_without_received_close_is_still_abnormal_eof(self):
        error = ConnectionClosedError(None, Close(1011, SECRET))
        self.assertIn("received_close=none,sent_close=1011,abnormal_eof=true", describe_transport_error(error))

    def test_cause_takes_precedence_over_context(self):
        error = RuntimeError(SECRET)
        error.__context__ = EOFError(SECRET)
        error.__cause__ = ConnectionResetError(54, SECRET)
        self.assertEqual(describe_transport_error(error),
            "chain=RuntimeError <- ConnectionResetError[errno=54]")

    def test_suppressed_context_is_not_included(self):
        error = RuntimeError(SECRET)
        error.__context__ = EOFError(SECRET)
        error.__suppress_context__ = True
        self.assertEqual(describe_transport_error(error), "chain=RuntimeError")

    def test_cycle_and_long_chains_are_bounded(self):
        first, second = RuntimeError(SECRET), EOFError(SECRET)
        first.__cause__, second.__cause__ = second, first
        self.assertEqual(describe_transport_error(first),
            "chain=RuntimeError <- EOFError[abnormal_eof=true] <- [cycle]")
        errors = [RuntimeError(SECRET) for _ in range(7)]
        for parent, child in zip(errors, errors[1:]):
            parent.__cause__ = child
        result = describe_transport_error(errors[0])
        self.assertEqual(result.count("RuntimeError"), 5)
        self.assertTrue(result.endswith(" <- [truncated]"))

    def test_untrusted_numeric_attributes_are_never_formatted(self):
        class Unprintable:
            def __str__(self):
                raise AssertionError("must not format arbitrary attribute values")
            __repr__ = __str__

        error = ConnectionClosedError(None, None)
        error.errno = Unprintable()
        error.winerror = SECRET
        error.rcvd = SimpleNamespace(code=Unprintable(), reason=SECRET)
        error.sent = SimpleNamespace(code=True, reason=SECRET)
        self.assertEqual(describe_transport_error(error),
            "chain=ConnectionClosedError[errno=unknown,winerror=unknown,"
            "received_close=unknown,sent_close=unknown,abnormal_eof=false]")

    def test_exception_messages_and_attribute_getter_failures_are_not_formatted(self):
        class UnprintableError(RuntimeError):
            def __str__(self):
                raise AssertionError("must not format the exception")
            __repr__ = __str__

            @property
            def errno(self):
                raise RuntimeError(SECRET)

        self.assertEqual(describe_transport_error(UnprintableError(SECRET)), "chain=UnprintableError")

    def test_integer_subclass_hooks_and_oversized_numbers_are_ignored(self):
        class UnprintableInt(int):
            def __int__(self):
                raise AssertionError("must not call custom conversion")
            def __str__(self):
                raise AssertionError("must not call custom formatting")

        error = RuntimeError(SECRET)
        error.errno = UnprintableInt(54)
        error.winerror = 10**5000
        self.assertEqual(describe_transport_error(error), "chain=RuntimeError[errno=54,winerror=unknown]")

    def test_dynamic_class_names_cannot_inject_text(self):
        error_type = type(SECRET, (RuntimeError,), {})
        self.assertEqual(describe_transport_error(error_type(SECRET)), "chain=Exception")


class SessionRotationTests(unittest.TestCase):
    def test_abnormal_eof_is_not_a_normal_rotation(self):
        for message in (None, "no close frame received or sent", "GoAway; no close frame received or sent"):
            with self.subTest(message=message):
                self.assertFalse(is_expected_session_rotation(message))

    def test_existing_other_rotation_markers_are_preserved(self):
        for message in ("GoAway", "session duration exceeded", "received 1008 (policy violation)",
                        "connection aborted because the client failed to close"):
            with self.subTest(message=message):
                self.assertTrue(is_expected_session_rotation(message))
        self.assertFalse(is_expected_session_rotation("connection reset by peer"))


if __name__ == "__main__":
    unittest.main()

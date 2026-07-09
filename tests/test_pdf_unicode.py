"""Tests for PDF Unicode hardening (Task H.2).

fpdf2's built-in Helvetica core font is latin-1 only, so any non-latin-1
character (em dash, curly quotes, accented letters, ellipsis, bullet) passed to
a draw call raises FPDFUnicodeEncodingException and crashes PDF generation.
Session notes / weekly summaries / expense descriptions are routinely pasted
from Notes/Mail and contain exactly these characters.

`generate_pdf` now neutralizes non-latin-1 text at the PDF boundary BY FIELD
CLASS:
  * FREE-TEXT (line-item descriptions, terms): transliterate SILENTLY.
  * IDENTITY / ADDRESS / PAYMENT (names, addresses, payment refs): transliterate
    BUT emit a stderr warning naming each field that was actually altered.

These tests exercise the real `generate_pdf` call path and never touch real
files (everything lives under a TemporaryDirectory).
"""

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from decimal import Decimal
from pathlib import Path

from fpdf.errors import FPDFUnicodeEncodingException

import invoice


def _base_config():
    """A minimal, fully-ASCII config so any warning must come from test input."""
    return {
        "invoice_header": {"title": "INVOICE", "logo_path": ""},
        "payee": {
            "name": "Zero Delta LLC",
            "address": "123 Main St",
            "city": "Reston",
            "state": "VA",
            "zip": "20190",
            "email": "billing@example.com",
            "phone": "555-0100",
        },
        "clients": [
            {
                "name": "Acme Corp",
                "address": "456 Market St",
                "city": "Arlington",
                "state": "VA",
                "zip": "22201",
                "contact": "ap@acme.example",
            }
        ],
        "payment": {
            "bank_name": "First National",
            "routing": "021000021",
            "account": "12345678",
            "description": "Wire transfer preferred",
        },
    }


class PdfUnicodeTests(unittest.TestCase):
    def _generate(self, config, line_items, client=None, **kwargs):
        """Run the real generate_pdf into a temp file, capturing stderr.

        Returns (total, pdf_path_exists, stderr_text).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "invoice.pdf"
            buf = io.StringIO()
            with redirect_stderr(buf):
                total = invoice.generate_pdf(
                    "2026-0001",
                    "2026-07-08",
                    config,
                    line_items,
                    str(out),
                    client=client,
                    **kwargs,
                )
            return total, out.exists(), buf.getvalue()

    # (a) FREE-TEXT with non-latin-1 chars must NOT crash, must return a total,
    #     and must write a PDF. This is the actual crash trigger.
    def test_free_text_description_with_unicode_does_not_crash(self):
        config = _base_config()
        line_items = [
            {
                # em dash, curly double quotes, accented letter, ellipsis, bullet
                "description": "Consulting — “phase 1” résumé review… • done",
                "hours": Decimal("2"),
                "rate": Decimal("100.00"),
                "amount": Decimal("200.00"),
            }
        ]

        try:
            total, wrote_pdf, _ = self._generate(config, line_items)
        except FPDFUnicodeEncodingException as exc:  # pragma: no cover
            self.fail(f"generate_pdf crashed on Unicode free-text: {exc}")

        self.assertIsInstance(total, Decimal)
        self.assertEqual(total, Decimal("200.00"))
        self.assertTrue(wrote_pdf, "PDF file was not written")

    # (a-cont) Free-text transliteration is SILENT — no warning for descriptions.
    def test_free_text_description_emits_no_warning(self):
        config = _base_config()
        line_items = [
            {
                "description": "Résumé review — final",
                "hours": Decimal("1"),
                "rate": Decimal("100.00"),
                "amount": Decimal("100.00"),
            }
        ]

        _total, wrote_pdf, stderr = self._generate(config, line_items)

        self.assertTrue(wrote_pdf)
        self.assertEqual(stderr, "", f"free-text should not warn, got: {stderr!r}")

    # (b) IDENTITY field (payee name) with a non-latin-1 char must succeed AND
    #     emit a warning that names the altered field.
    def test_identity_field_unicode_emits_named_warning(self):
        config = _base_config()
        config["payee"]["name"] = "Zéro Delta LLC"  # accented identity field
        line_items = [
            {
                "description": "Consulting",
                "hours": Decimal("1"),
                "rate": Decimal("100.00"),
                "amount": Decimal("100.00"),
            }
        ]

        total, wrote_pdf, stderr = self._generate(config, line_items)

        self.assertEqual(total, Decimal("100.00"))
        self.assertTrue(wrote_pdf)
        self.assertIn("Warning", stderr)
        self.assertIn("payee", stderr.lower())
        # The warning should show the original value so the user can spot it.
        self.assertIn("Z", stderr)

    # (b-cont) A PAYMENT-reference field warning names the payment field.
    def test_payment_field_unicode_emits_named_warning(self):
        config = _base_config()
        config["payment"]["account"] = "1234–5678"  # en dash in account ref
        line_items = [
            {
                "description": "Consulting",
                "hours": Decimal("1"),
                "rate": Decimal("100.00"),
                "amount": Decimal("100.00"),
            }
        ]

        _total, wrote_pdf, stderr = self._generate(config, line_items)

        self.assertTrue(wrote_pdf)
        self.assertIn("Warning", stderr)
        self.assertIn("payment account", stderr.lower())

    # (c) Pure-ASCII input across every field class must emit NO warning
    #     (behavior-preserving) and still produce a PDF.
    def test_pure_ascii_input_emits_no_warning(self):
        config = _base_config()
        line_items = [
            {
                "description": "Consulting - phase 1 (final)",
                "hours": Decimal("3"),
                "rate": Decimal("150.00"),
                "amount": Decimal("450.00"),
            },
            {
                # flat-fee / expense row (hours == 0 and rate == 0)
                "description": "Travel reimbursement",
                "hours": Decimal("0"),
                "rate": Decimal("0"),
                "amount": Decimal("42.00"),
            },
        ]

        total, wrote_pdf, stderr = self._generate(
            config, line_items, payment_terms="Net 30", payment_description="ACH only"
        )

        self.assertEqual(total, Decimal("492.00"))
        self.assertTrue(wrote_pdf)
        self.assertEqual(stderr, "", f"ASCII input must not warn, got: {stderr!r}")


class Latin1SafeHelperTests(unittest.TestCase):
    """Direct unit coverage of the _latin1_safe helper contract."""

    def test_maps_common_typographic_chars_to_ascii(self):
        self.assertEqual(invoice._latin1_safe("a — b"), "a - b")
        self.assertEqual(invoice._latin1_safe("a – b"), "a - b")
        self.assertEqual(invoice._latin1_safe("“quote”"), '"quote"')
        self.assertEqual(invoice._latin1_safe("it’s"), "it's")
        self.assertEqual(invoice._latin1_safe("wait…"), "wait...")
        self.assertEqual(invoice._latin1_safe("• item"), "* item")

    def test_ascii_passes_through_unchanged(self):
        s = "Plain ASCII text 123 - (final)."
        self.assertEqual(invoice._latin1_safe(s), s)

    def test_non_str_input_returned_as_is(self):
        self.assertEqual(invoice._latin1_safe(42), 42)
        self.assertIsNone(invoice._latin1_safe(None))

    def test_unrepresentable_char_becomes_replacement_not_crash(self):
        # A CJK character has no latin-1 codepoint and is not in the typographic
        # map, so it must degrade to "?" rather than raising.
        out = invoice._latin1_safe("hi 中")
        self.assertTrue(out.startswith("hi "))
        self.assertNotIn("中", out)


if __name__ == "__main__":
    unittest.main()

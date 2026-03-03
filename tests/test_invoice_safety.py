import unittest

import invoice


class InvoiceSafetyTests(unittest.TestCase):
    def test_sanitize_filename_component_blocks_traversal_chars(self):
        value = invoice._sanitize_filename_component("../Acme/../../Q1:2026", "Client")
        self.assertEqual(value, "Acme_.._.._Q1_2026")

    def test_validate_invoice_number_rejects_invalid_format(self):
        with self.assertRaises(Exception):
            invoice._validate_invoice_number("../../2026-0001")

    def test_csv_safe_prefixes_formula_cells(self):
        self.assertEqual(invoice._csv_safe("=2+2"), "'=2+2")
        self.assertEqual(invoice._csv_safe("+SUM(A1:A2)"), "'+SUM(A1:A2)")
        self.assertEqual(invoice._csv_safe("normal text"), "normal text")

    def test_split_address_lines_supports_literal_backslash_n(self):
        lines = invoice._split_address_lines("123 Main St\\nPO Box 456")
        self.assertEqual(lines, ["123 Main St", "PO Box 456"])


if __name__ == "__main__":
    unittest.main()

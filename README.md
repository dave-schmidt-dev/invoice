# invoice

Command-line invoice generator for creating PDF invoices and tracking them in a CSV log.

## Features

- Interactive invoice creation (`invoice.py new`)
- PDF generation with line items and totals
- Client profiles and reusable config in `~/.invoice_config.json`
- Quick file shortcuts for opening the configured ledger and a specific invoice PDF
- Invoice status tracking (`Draft`, `Sent`, `Paid`, `Overdue`)
- Filtered listing (`invoice.py list --status sent`)
- Safer file handling: atomic writes and lock-protected CSV updates
- Money handling with `Decimal` for consistent currency math

## Requirements

- Python 3.8+
- Dependencies in `requirements.txt`

Install:

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Quick Start

1. Configure payee/client/payment/storage:

```bash
./venv/bin/python invoice.py config
```

2. Create an invoice:

```bash
./venv/bin/python invoice.py new
```

3. List invoices:

```bash
./venv/bin/python invoice.py list
```

4. Open the configured invoice ledger:

```bash
./venv/bin/python invoice.py --ledger
```

5. Open a specific invoice PDF:

```bash
./venv/bin/python invoice.py --invoice 2026-0001
```

6. Filter by status:

```bash
./venv/bin/python invoice.py list --status sent
```

7. Update status:

```bash
./venv/bin/python invoice.py status 2026-0001 Paid
```

## Using `invoice-wrapper`

The repo includes `invoice-wrapper`, which runs `invoice.py` through the virtual environment automatically.

Examples:

```bash
./invoice-wrapper --ledger
./invoice-wrapper --invoice 2026-0001
./invoice-wrapper list --status all
./invoice-wrapper new
```

## Commands

```text
invoice.py --ledger
invoice.py --invoice INVOICE_NUMBER
invoice.py config
invoice.py new [--date YYYY-MM-DD]
invoice.py list [--status all|draft|sent|paid|overdue]
invoice.py status INVOICE_NUMBER {draft|sent|paid|overdue}
```

## Configuration

Runtime config is stored at:

```text
~/.invoice_config.json
```

See [`config.example.json`](config.example.json) for a full template.

Key config sections:

- `invoice_header`: title and optional logo path
- `payee`: your business/contact details
- `clients`: one or more client profiles
- `payment`: bank/payment instructions shown on invoice
- `storage`: ledger path and invoice output directory

The `storage.ledger_file` value is the exact file opened by `invoice.py --ledger`. Existing configs that still use `storage.csv_file` are migrated automatically.

Address fields support literal `\n` in input and are rendered as separate lines in the PDF.

## CSV Log Format

| Column | Description | Example |
|---|---|---|
| `invoice_number` | Invoice identifier | `2026-0001` |
| `date` | Invoice date (ISO-8601) | `2026-03-03` |
| `payee_name` | Payee name/company | `Acme Corporation` |
| `payer_name` | Client name/company | `Globex International` |
| `line_items` | Flattened line-item summary | `Website redesign (12 hrs @ $150.00/hr)` |
| `total` | Total amount (2 decimals, no currency symbol) | `2175.00` |
| `pdf_file` | Full path to generated PDF | `/Users/you/invoices/Acme_Invoice_2026-0001.pdf` |
| `status` | Invoice lifecycle status | `Draft` |

## Security and Data-Safety Notes

- Invoice numbers are validated to safe characters.
- Filename components are sanitized before writing PDFs.
- CSV writes are lock-protected to reduce race conditions.
- Critical rewrites use atomic replace patterns.
- CSV text fields are protected against spreadsheet formula injection.

## Known Limitations

- On macOS, Apple Mail compose includes attachment automatically.
- On Linux/Windows, the default mail client opens via `mailto:` and may require manual attachment.

## Development

Run tests:

```bash
./venv/bin/python -m unittest discover -s tests -v
```

## License

See [LICENSE](LICENSE).

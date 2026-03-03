# invoice

A command-line tool for generating professional PDF invoices and tracking them in a CSV log.

## Features

- Config lives in `~/.invoice_config.json` — works from **any directory** in your terminal
- Stores payee (you) and payer (client) info once; reused for every invoice
- CSV log and PDF output paths are **configurable** — point them anywhere on your machine
- Prompts for multiple line items per invoice (description, hours, and pay rate)
- Generates a clean, professional PDF invoice
- Appends every invoice to a CSV log with an auto-incremented invoice number
- Includes a `list` command to view all past invoices at a glance

## Requirements

- Python 3.8+
- Install dependencies:

```bash
pip install -r requirements.txt
```

## Quick Start

### 1. Configure payee & payer information

Run the config wizard once (or any time you need to update your info):

```bash
python invoice.py config
```

This saves your information to `~/.invoice_config.json` so the tool works from any directory.  
The wizard will also ask where you want your CSV log and PDF output stored (defaults to `~/invoices/`).

See `config.example.json` for the expected structure.

### 2. Create a new invoice

```bash
python invoice.py new
```

You will be prompted to enter one or more line items:

```
Creating Invoice #0001  (2024-05-01)

=== Invoice Line Items ===
Enter each project / task below. Leave description blank to finish.

Description (blank to finish): Website redesign
  Hours: 12
  Rate ($/hr): 150
  → Amount: $1,800.00

Description (blank to finish): SEO audit
  Hours: 3
  Rate ($/hr): 125
  → Amount: $375.00

Description (blank to finish):

✓  Invoice #0001 saved to: ~/invoices/invoice_0001_2024-05-01.pdf
✓  Total due: $2,175.00
✓  CSV log updated: ~/invoices/invoices.csv
```

### 3. List all invoices

```bash
python invoice.py list
```

```
#       Date          Payer                           Total   PDF
--------------------------------------------------------------------------------
0001    2024-05-01    Acme Corp                    $2175.00   ~/invoices/invoice_0001_2024-05-01.pdf
```

## Running from anywhere

Because the config is stored in your home directory, you can run the tool from any folder:

```bash
# From a project directory, a temp folder, anywhere — same invoice history every time
cd /some/other/directory
python /path/to/invoice.py new
```

Or add a shell alias for convenience:

```bash
alias invoice="python /path/to/invoice.py"
```

Then simply run `invoice new`, `invoice list`, etc. from any location.

## File Structure

```
invoice.py            # CLI entry point
requirements.txt      # Python dependencies
config.example.json   # Example config structure (safe to commit)
~/.invoice_config.json   # Your real config — stored in home dir, never in the repo
~/invoices/           # Default output directory (configurable)
  invoices.csv        # CSV log of all invoices
  invoice_0001_*.pdf  # Generated PDF files
```

## Configuring storage paths

During `invoice.py config` you are asked:

```
=== Storage Paths ===
Invoice CSV log path [~/invoices/invoices.csv]:
PDF output directory [~/invoices]:
```

You can point these anywhere — a Dropbox folder, an iCloud Drive directory, etc. — so your invoice history is automatically backed up.

## CSV Log Format

| Column | Description |
|---|---|
| `invoice_number` | Zero-padded invoice number (e.g. `0001`) |
| `date` | ISO-8601 date the invoice was created |
| `payee_name` | Your name / company |
| `payer_name` | Client name / company |
| `line_items` | Semicolon-separated summary of line items |
| `total` | Total amount due |
| `pdf_file` | Path to the generated PDF |

## License

See [LICENSE](LICENSE).

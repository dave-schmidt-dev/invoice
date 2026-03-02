# invoice

A command-line tool for generating professional PDF invoices and tracking them in a CSV log.

## Features

- Stores payee (you) and payer (client) info in `~/.invoice_config.json` — found from any working directory
- **Configurable data directory** — choose where the CSV log and PDFs are stored (defaults to `~/.invoice/`)
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

## Installation (run from anywhere in the terminal)

Make the script executable and create a symlink (or copy) somewhere on your `PATH`:

```bash
chmod +x invoice.py

# Option A — symlink into /usr/local/bin (macOS/Linux, requires sudo)
sudo ln -sf "$(pwd)/invoice.py" /usr/local/bin/invoice

# Option B — copy into ~/bin (no sudo needed; add ~/bin to PATH if not already there)
mkdir -p ~/bin
cp invoice.py ~/bin/invoice
# Add to ~/.zshrc or ~/.bashrc if ~/bin is not on PATH:
#   export PATH="$HOME/bin:$PATH"
```

After this you can run `invoice` from any directory.

## Quick Start

### 1. Configure payee, payer & data directory

Run the config wizard once (or any time you need to update your info):

```bash
invoice config
```

You will be prompted for payee/payer details and — importantly — the **data directory**
where the CSV log and generated PDFs will be stored.  The default is `~/.invoice/` so
the same invoice history is accessible no matter which directory you are in.

Your configuration is saved to `~/.invoice_config.json`.  
See `config.example.json` for the expected structure.

### 2. Create a new invoice

```bash
invoice new
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

✓  Invoice #0001 saved to: /Users/you/.invoice/invoices/invoice_0001_2024-05-01.pdf
✓  Total due: $2,175.00
✓  CSV log updated: /Users/you/.invoice/invoices.csv
```

### 3. List all invoices

```bash
invoice list
```

```
#       Date          Payer                           Total   PDF
--------------------------------------------------------------------------------
0001    2024-05-01    Acme Corp                    $2175.00   /Users/you/.invoice/invoices/invoice_0001_2024-05-01.pdf
```

## File layout

| Path | Contents |
|---|---|
| `~/.invoice_config.json` | Payee, payer, banking info, and `data_dir` setting |
| `~/.invoice/invoices.csv` | Ledger: number, date, parties, line items, total, PDF path *(default location)* |
| `~/.invoice/invoices/` | Generated PDF files *(default location)* |

The `data_dir` value in `~/.invoice_config.json` controls where the CSV and PDFs live.
You can set it to any absolute path (e.g. `~/Documents/Invoices`) during `invoice config`.

## CSV Log Format

| Column | Description |
|---|---|
| `invoice_number` | Zero-padded invoice number (e.g. `0001`) |
| `date` | ISO-8601 date the invoice was created |
| `payee_name` | Your name / company |
| `payer_name` | Client name / company |
| `line_items` | Semicolon-separated summary of line items |
| `total` | Total amount due |
| `pdf_file` | Absolute path to the generated PDF |

## License

See [LICENSE](LICENSE).

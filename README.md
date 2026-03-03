# 📄 invoice

**A modern command-line tool for generating professional PDF invoices** 🚀

[![Python](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Generate clean and simple PDF invoices with a simple CLI interface.

## ✨ Features

- **📁 Portable Config**: Lives in `~/.invoice_config.json` — works from **any directory** in your terminal
- **🔄 Reusable Info**: Stores payee (you) and payer (client) info once; reused for every invoice
- **📂 Flexible Storage**: CSV log and PDF output paths are **configurable** — point them anywhere on your machine
- **📝 Multiple Line Items**: Prompts for multiple line items per invoice (description, hours, and pay rate)
- **📄 Professional PDFs**: Generates clean, professional PDF invoices with proper formatting
- **📊 CSV Logging**: Appends every invoice to a CSV log with year-based invoice numbers
- **🔍 Invoice Listing**: Includes a `list` command to view all past invoices at a glance

### 🆕 Recent Improvements

- **📍 Two-line street addresses**: Use `\n` to separate street address from PO Box/suite
- **🔢 Year-based invoice numbers**: Format `YYYY-####` (e.g., `2026-0001`)
- **💬 Per-invoice payment descriptions**: Customize payment instructions for each invoice
- **📁 Client-based filenames**: `ClientName_Invoice_InvoiceNumber.pdf` format
- **🎨 Enhanced PDF formatting**: Better spacing, alignment, and visual hierarchy
- **✉️ Email integration**: Automatic Apple Mail opening with pre-filled emails
- **📊 Status tracking**: Track invoice status (Draft/Sent/Paid/Overdue)
- **🔄 Automatic venv**: No manual virtual environment activation needed
- **🐛 Bug fixes**: All formatting and alignment issues resolved

## Requirements

- Python 3.8+
- Install dependencies:

```bash
pip install -r requirements.txt
```

**🐍 Virtual Environment Recommended**:

If your system Python is managed by a package manager (like Homebrew on macOS), use a virtual environment:

```bash
# Create and activate virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies in the virtual environment
pip install -r requirements.txt
```

This prevents conflicts with system-wide Python packages and ensures a clean environment.

## 🚀 Quick Start

### 1️⃣ Configure payee & payer information

Run the config wizard once (or any time you need to update your info):

```bash
python invoice.py config
```

This saves your information to `~/.invoice_config.json` so the tool works from **any directory**.  
The wizard will also ask where you want your CSV log and PDF output stored (defaults to `~/invoices/`).

**💡 Address Format Tip**: For two-line street addresses (e.g., street + PO Box), use `\n` to separate lines:

```
Street address (use \n for separate lines, e.g., '123 Main St\nPO Box 456'): 123 Main St\nPO Box 456
```

See [`config.example.json`](config.example.json) for the expected structure.

### 2️⃣ Create a new invoice

```bash
python invoice.py new
```

You will be prompted to enter invoice details and line items:

```
--- Invoice Setup ---
Default: Invoice #2026-0001 dated 2026-03-03
Invoice number (press Enter to use default): 
Invoice date (YYYY-MM-DD, press Enter for today): 
Payment description (press Enter to use default): Please pay via ACH or check

--- Creating Invoice #2026-0001 dated 2026-03-03 ---

Select a client:
  1. Acme Corporation
  2. Globex International
Client number: 1

Payment Terms:
  1. Net 15
  2. Net 30
  3. Upon Receipt
  4. Custom
Select payment terms [2]: 2

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

✓  Invoice #2026-0001 saved to: ~/invoices/Acme_Corporation_Invoice_2026-0001.pdf
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
2026-0001 2026-03-03  Acme Corporation             $2175.00   ~/invoices/Acme_Corporation_Invoice_2026-0001.pdf
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

## 📊 CSV Log Format

| Column | Description | Example |
|--------|-------------|---------|
| `invoice_number` | Year-based invoice number | `2026-0001` |
| `date` | ISO-8601 date the invoice was created | `2026-03-03` |
| `payee_name` | Your name / company | `Acme Corporation` |
| `payer_name` | Client name / company | `Globex International` |
| `line_items` | Semicolon-separated summary of line items | `Website redesign (12 hrs @ $150.00/hr); SEO audit (3 hrs @ $125.00/hr)` |
| `total` | Total amount due | `$2175.00` |
| `pdf_file` | Path to the generated PDF | `/Users/you/invoices/Acme_Corporation_Invoice_2026-0001.pdf` |

## Known Issues & Limitations

### Current Limitations

- **Email attachments on macOS**: PDF must be manually attached (AppleScript limitation)
- **Windows/Linux email**: Falls back to mailto: link (manual attachment needed)
- **Status values**: Only Draft/Sent/Paid/Overdue supported (by design)

### Workarounds

1. **Email attachments**: One-click attach in Apple Mail after email opens
2. **Long PDF paths**: Dynamic column width handles this automatically
3. **Virtual environment**: Use the `invoice` command (automatic activation)

## License

See [LICENSE](LICENSE).

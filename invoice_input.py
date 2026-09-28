"""Money parsing, interactive input and file-opening helpers for invoice.py (moved verbatim)."""

import copy
import os
import subprocess
import sys
import urllib.parse
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import click


_VALID_LOGO_EXTS = {".png", ".jpg", ".jpeg"}


PAYMENT_TERMS_CHOICES = ["Net 15", "Net 30", "Upon Receipt", "Custom"]


MONEY_PRECISION = Decimal("0.01")


_DEFAULT_CLIENT = {
    "name": "",
    "address": "",
    "city": "",
    "state": "",
    "zip": "",
    "contact": "",
}


def _to_decimal(value, field_name):
    """Convert a user-supplied numeric value into Decimal."""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise click.ClickException(f"Invalid numeric value for {field_name}: {value!r}") from exc


def _to_money_decimal(value, field_name="amount"):
    """Convert a value to a currency Decimal rounded to cents."""
    return _to_decimal(value, field_name).quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP)


def _prompt_decimal(prompt_text, field_name, minimum):
    """Prompt until a valid Decimal >= minimum is provided."""
    while True:
        raw_value = click.prompt(prompt_text, type=str).strip()
        try:
            value = _to_decimal(raw_value, field_name)
        except click.ClickException:
            click.echo(f"Invalid {field_name.lower()}. Enter a numeric value.")
            continue
        if value < minimum:
            click.echo(f"{field_name} must be at least {minimum}.")
            continue
        return value


def _open_path(path):
    """Open a file in the platform-default application."""
    target = Path(path).expanduser()
    if not target.exists():
        raise click.ClickException(f"Path not found: {target}")
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(target)], check=True)
        elif sys.platform == "win32":
            os.startfile(str(target))  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(target)], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise click.ClickException(f"Could not open '{target}': {exc}") from exc
    return target


def _open_email_client(client_email, subject, body, pdf_path):
    """Open a compose window in the default mail client."""
    recipient = urllib.parse.quote(client_email, safe="@._+-")
    encoded_subject = urllib.parse.quote(subject)
    encoded_body = urllib.parse.quote(body)
    mailto_url = f"mailto:{recipient}?subject={encoded_subject}&body={encoded_body}"

    if sys.platform == "darwin":
        script = """
on run argv
    set recipientAddress to item 1 of argv
    set messageSubject to item 2 of argv
    set messageBody to item 3 of argv
    set attachmentPath to item 4 of argv

    tell application "Mail"
        set newMessage to make new outgoing message with properties {subject:messageSubject, content:messageBody}
        tell newMessage
            make new to recipient at end of to recipients with properties {address:recipientAddress}
            tell content
                make new attachment with properties {file name:(POSIX file attachmentPath as alias)}
            end tell
            activate
        end tell
    end tell
end run
""".strip()
        subprocess.run(
            ["osascript", "-e", script, client_email, subject, body, str(pdf_path)],
            check=True,
        )
        return "apple_mail"

    if sys.platform == "win32":
        os.startfile(mailto_url)  # type: ignore[attr-defined]
        return "mailto_windows"

    subprocess.run(["xdg-open", mailto_url], check=True)
    return "mailto"


def _prompt_client_info(existing=None):
    """Interactively prompt for a single client's info. Returns a client dict."""
    c = copy.deepcopy(existing or _DEFAULT_CLIENT)
    c["name"] = click.prompt("Client name or company", default=c.get("name") or "")
    c["contact"] = click.prompt("Contact name", default=c.get("contact") or "")
    c["address"] = click.prompt(
        "Client street address (use \\n for separate lines, e.g., '123 Main St\\nPO Box 456')", 
        default=c.get("address") or ""
    )
    c["city"] = click.prompt("Client city", default=c.get("city") or "")
    c["state"] = click.prompt("Client state", default=c.get("state") or "")
    c["zip"] = click.prompt("Client ZIP code", default=c.get("zip") or "")
    return c


def get_line_items():
    """Interactively collect line items from the user."""
    line_items = []
    click.echo(
        "\n=== Invoice Line Items ===\n"
        "Enter each project / task below. Leave description blank to finish.\n"
    )

    while True:
        description = click.prompt(
            "Description (blank to finish)", default="", show_default=False
        )
        if not description:
            if not line_items:
                click.echo("At least one line item is required.")
                continue
            break

        hours = _prompt_decimal("  Hours", "Hours", Decimal("0"))
        rate = _prompt_decimal("  Rate ($/hr)", "Rate", Decimal("0"))
        amount = _to_money_decimal(hours * rate)

        line_items.append(
            {
                "description": description,
                "hours": hours,
                "rate": rate,
                "amount": amount,
            }
        )
        click.echo(f"  → Amount: ${amount:,.2f}\n")

    return line_items

# Invariants — invoice

> System contract. The harvest tool reads `area:` globs to map HISTORY bug entries
> to invariants. Per-project convention (commit prefix, invariant refs) is declared
> in this project's CLAUDE.md/README, not globally.

### INV-1 — No PII or secrets are ever committed to the repository or written to logs.
area: ["**/*"]
gate_test: tests/test_pii_hook.py
threshold: 3
rationale: Real client identities, addresses, rates, invoice numbers, banking routing/account numbers, and any contents of ~/.zd.db, ~/.invoice_config.json, or generated PDFs must stay out of version control (README "No PII in the project" hard rule; enforced by hooks/pre-commit). Prevents confidentiality breach of a real engagement's data.

### INV-2 — An invoice is atomic in its authoritative store, and the other stores converge to it with no partial state that causes double-billing or silent overwrite.
area: ["zd.py", "invoice.py"]
gate_test: tests/test_invoice_transaction_integrity.py
threshold: 3
rationale: True atomicity across two files + a DB is impossible, so atomicity is defined per authoritative store. For zd-generated invoices the zd SQLite DB is authoritative; the CSV ledger and PDF are projections that must converge to it (auto-reconciled on the next run). For standalone invoice.py invoices the CSV ledger is authoritative. No partial-write state may leave sessions billable twice or overwrite an existing invoice. Prevents double-billing, orphaned invoices, and cross-ledger desync.

### INV-3 — A sent or paid invoice's stored total and billed line items are immutable except through an explicit regeneration that preserves the originally-billed rate.
area: ["zd.py", "invoice.py"]
gate_test: tests/test_invoice_immutability.py
threshold: 3
rationale: Regeneration must not recompute totals at the client's current rate, and editing an already-billed session/expense must not silently leave the parent invoice total stale. Prevents retroactive corruption of the historical financial record.

### INV-4 — Monetary values are computed and compared as Decimal with ROUND_HALF_UP, and the total shown for confirmation equals the total persisted to the PDF, CSV, and DB.
area: ["zd.py", "invoice.py"]
gate_test: tests/test_money_totals.py
threshold: 3
rationale: All money arithmetic and comparison uses Decimal/ROUND_HALF_UP; on-disk SQLite REAL columns are always read back through Decimal(str(v)), which is exact for money-magnitude values, so no float arithmetic ever touches a billed amount. Per-week rounding must not diverge from the total the user approves at the "Proceed?" prompt. Prevents approved-vs-actual mismatch and float drift in billed amounts.

### INV-5 — Invoice numbers are unique per year, and generating an invoice never overwrites an existing invoice's PDF file or ledger row.
area: ["invoice.py", "zd.py"]
gate_test: tests/test_invoice_numbering.py
threshold: 3
rationale: The uniqueness check must run before any durable write (PDF or CSV), and numeric ordering must not degrade to lexicographic string comparison. Prevents silent overwrite/loss of a prior invoice's PDF or ledger entry.

### INV-6 — Every write to the config, DB, or CSV ledger is atomic, lock-protected where the file is shared, and preceded by a timestamped backup.
area: ["zd.py", "invoice.py"]
gate_test: tests/test_write_safety.py
threshold: 3
rationale: All persistence paths (including zd's direct config/CSV patches) must go through atomic-replace + file-lock + backup, never a bare truncating write, and must fail closed rather than clobber a live file with a stub. Prevents partial-write corruption and config/ledger data loss.

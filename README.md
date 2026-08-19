# ESG RR Consolidation Portal — MVP

Clean local MVP for Receiving Report consolidation.

## What V1 does

1. Upload one RR PDF.
2. Run the deterministic RR parser.
3. Extract transactional item rows.
4. Group by `(RR reference number, normalized item description)`.
5. Sum KILOS, PALLET WEIGHT, NET WEIGHT, and QTY.
6. Show one consolidated item row per description.
7. Download the consolidated result as CSV.

No Google Sheets, database, AI, material splits, or certificate generation yet.

## Windows

Open PowerShell in this folder and run:

```powershell
.\start.ps1
```

Then open <http://127.0.0.1:4310>.

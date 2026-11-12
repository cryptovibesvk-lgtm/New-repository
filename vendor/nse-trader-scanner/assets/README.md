# Bundled index universes

These files were generated from official NSE constituent CSVs on 2026-11-01:

| File | Source |
|---|---|
| `nifty50.txt` | `https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv` |
| `nifty100.txt` | `https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv` |
| `nifty500.txt` | `https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv` |

The Nifty 500 source contained 501 unique symbols at retrieval time, which can
happen during index-transition periods. The repository preserves the official
snapshot rather than silently deleting a constituent.

Refresh snapshots with:

```bash
python scripts/update_reference_data.py
```

Review the diff before committing because index changes affect scanner output.

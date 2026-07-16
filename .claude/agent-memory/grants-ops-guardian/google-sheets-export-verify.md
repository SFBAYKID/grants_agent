---
name: google-sheets-export-verify
description: How to smoke-test Grant's Google Sheets export end-to-end on the droplet (create + trash a throwaway sheet)
metadata:
  type: reference
---

Grant's Google Sheets export (`grant_watch/google_sheets.py`, `create_sheet(...)`) is wired and
**verified working on the droplet 2026-07-14** — a real sheet was created in the "Grant Exports" shared
drive and trashed cleanly. So the droplet `.env` has valid `GOOGLE_SA_KEY_PATH` (a Content-Manager
service-account key) and `GRANT_EXPORTS_DRIVE_ID`, and the SA has Drive write access. (Values never read.)

**Reusable smoke-test (isolated Drive API check — no Slack, no poll/drip, no DB):** feed a throwaway
snippet to the droplet venv over stdin so nothing lands on droplet disk:

```
ssh -i ~/.ssh/grants_droplet -o IdentitiesOnly=yes "$GRANTS_DROPLET_USER@$GRANTS_DROPLET_HOST" \
  'cd /home/grantwatch/grants_agent && .venv/bin/python -' < local_snippet.py
```
The snippet: `load_dotenv("/home/grantwatch/grants_agent/.env")`, call
`google_sheets.create_sheet(title, cols, rows, "<any-truthy-slack-id>", "<share-email>")`, print STATE +
URL. Healthy result: `STATE: created` and a real `docs.google.com/spreadsheets/d/<id>` URL.

**Clean up so nothing lingers in the shared drive:** parse the id from the URL and trash it — resolve the
SA key with `google_sheets._key_path()` (the exact key the create used) rather than a hard-coded path, so
cleanup can't fail on a path mismatch:
```
build("drive","v3",credentials=creds,cache_discovery=False).files().update(
    fileId=sid, body={"trashed":True}, supportsAllDrives=True).execute()
```

Non-`created` states are truthful and diagnostic: `unconfigured` = env/key path wrong or google libs
missing on the droplet; `error` with an HTTP code = a Drive permission/config problem. Note
`create_sheet` shares with `sendNotificationEmail=False`, so the smoke-test never emails anyone.
See [[tenant-and-layout]].

# Accounts and internet hosting

The first implementation provides an owner account (`admin`) and one regular user
account (case-insensitive login; called `<user>` below). Credentials are created
locally, not shipped with the application. Run:

```powershell
python -m igris --setup-users
python -m igris --web
```

Setup prompts twice for each password (12–256 characters), without echoing it.
Existing accounts are never overwritten. Local recovery:

```powershell
python -m igris --reset-password <user>
```

Password changes and resets revoke all sessions belonging to that account.
The browser supports login, logout, password changes and an owner-only user list.
PBKDF2-SHA256 password records are salted; session tokens are stored as digests.
Sessions expire after 12 hours. Failed logins are limited by account and client IP.
POST operations require same-origin JSON and an authenticated CSRF token.

## Data and model configuration

`IGRIS_DATA_DIR` defaults to `~/.igris`, outside the repository, and holds all state.
It contains the owner's memory, sessions, personas, budget and jobs, `accounts.sqlite3`
and `users/<internal-id>/` with separate sessions, personas,
workspace and budget files. The owner retains the existing configured history,
workspace and multiagent jobs. Browser login sessions remember conversation and
persona selections independently. Requests are serialized per user, not globally.

Regular users default to the locally configured Ollama backend. Ollama must already
be available for model responses. Set `IGRIS_USER_BACKENDS` to `ollama`, `openai`,
`gemini`, or a comma-separated combination; `echo` is available for offline checks.
The free OpenAI-compatible cloud providers from `docs/free_models_plan.md`
(`openrouter`, `groq`, `cerebras`, `mistral`, `nvidia`, `cloudflare`, `sambanova`,
`cohere`) are registered backends and may also be listed in `IGRIS_USER_BACKENDS`.
Providers that may train on prompts never receive private
requests (attachments, workspace, memory or user context) unless
`IGRIS_ALLOW_TRAINING_PROVIDERS=1`; their keys live in the OS keyring. The default
remains `IGRIS_USER_BACKENDS=ollama`. Ranked routing reads `IGRIS_DATA_DIR/bench.sqlite3`
when it exists; without enough benchmark results it keeps configured order. The
privacy gate remains on. The Models panel reports unknown quota and rank values
honestly rather than estimating.
API credentials remain server-side using the existing IGRIS settings. OpenAI daily
budgets are separate per account and use `IGRIS_OPENAI_DAILY_BUDGET_USD`.
Host CLI backends and all multiagent endpoints remain owner-only until OS-level
execution isolation is implemented. Folder checks alone cannot isolate CLI agents.
The regular user's workspace supports the existing read-only file tools. Private
uploads are separately stored in `files.sqlite3`, scoped by user ID, including for
the owner. Original filenames are metadata and never filesystem paths. The web
sidebar supports upload, list, download and deletion. Downloads always use attachment
disposition and an octet-stream content type. Default quotas are 25 MiB per file,
500 MiB live content and 500 files per user (`IGRIS_FILE_MAX_MB`,
`IGRIS_FILE_QUOTA_MB`, `IGRIS_FILE_MAX_COUNT`). Duplicate names receive independent IDs.
Deletion frees the logical quota; SQLite may retain freed pages for reuse.

Models receive `list_uploads` and `read_upload` tools bound to the current user.
`read_upload` returns plain text from UTF-8 text/code/data, ZIP archives (every
readable member), Word DOCX, Excel XLSX, PowerPoint PPTX and PDF files with a text
layer (`igris/doctext.py`, standard library only; scanned PDFs have no OCR). Each call
returns up to 20,000 characters with a truncation flag and an offset for the next
part. Other formats can be stored and downloaded. Upload/read/download/delete events are kept separately per owner
(latest 200 stored, latest 20 displayed). Explicit shared folders remain pending.

## Background tasks

The sidebar supports user-owned text tasks, results and cancellation. They use the
account's configured slow model router and tools, with an ephemeral transcript;
they never switch or append to the active chat. This is separate from the existing
owner-only multiagent code execution system. Regular users still cannot run host CLIs.

`tasks.sqlite3` stores ownership, input, status, timestamps and output. The queue
runs up to `IGRIS_TASK_WORKERS` tasks globally (default 2, allowed 1–4), at most one
per owner, with at most 10 pending/running tasks per owner. Model calls within an
account serialize with that account's chat; task list/cancel endpoints stay responsive.
The latest approximately 100 completed records are retained alongside pending tasks.
Outputs above 100,000 characters are marked as truncated.

Queued cancellation prevents execution. Running cancellation waits for the backend
call to return, then discards the result; the worker slot remains occupied until then.
On restart, queued work resumes and previously running work becomes interrupted,
without automatically repeating a model call. Run only one Igris queue-owning process
against a data directory. Existing multiagent concurrency is configured separately.

## Internet access

Internet deployment is deferred at the owner's request. The application still binds to
`127.0.0.1:8770`. An HTTPS reverse proxy or authenticated tunnel must terminate TLS
and forward to this local address, preserving the public Host header.

Set `IGRIS_PUBLIC_ORIGIN` to the actual HTTPS origin, for example
`https://igris.example.com`, before starting Igris. This enforces the public Host
and Origin and adds Secure to session cookies. This setting does not provision a
domain, certificate or tunnel. Do not expose port 8770 directly to the internet.
Forwarded headers are not trusted for identity; behind a proxy, IP login limits
are shared at the proxy address, while account limits remain separate.

No public endpoint or real passwords have been created by this change.

### Go-live checklist (owner, when exposure is approved)

Everything above this line is implemented and tested. The steps below are manual and
must be done in order; skipping one leaves the server either unreachable or unsafe.

1. **Create the accounts locally.** Run `python -m igris --setup-users` on the host.
   Passwords are read without echo, need 12–256 characters, are confirmed twice and are
   never accepted as command line arguments. Store them in a password manager.
2. **Choose the public address.** Pick a domain or an authenticated tunnel (for
   example Cloudflare Tunnel or Tailscale Funnel). Igris keeps listening only on
   `127.0.0.1:8770`; never forward that port from the router.
3. **Put an HTTPS reverse proxy in front.** The proxy terminates TLS with a valid
   certificate, forwards to `http://127.0.0.1:8770`, preserves the public `Host`
   header and passes `Origin` unchanged. Plain HTTP on the public side must redirect
   to HTTPS or be closed.
4. **Set `IGRIS_PUBLIC_ORIGIN`.** Use the exact origin, for example
   `IGRIS_PUBLIC_ORIGIN=https://igris.example.com` (scheme and host, optional port,
   no path). Igris then rejects any other `Host` or `Origin` with 403, including
   `127.0.0.1`, so local access also goes through the proxy from now on. Cookies gain
   `Secure`. Start Igris and confirm `GET /` through the proxy shows the login page.
5. **Verify from another network.** From a phone on mobile data: log in as
   `<user>`, confirm the Multiagent tab and `/api/admin/users` return 403, log
   out, then log in as `admin`. Five wrong passwords must lock that account for
   15 minutes (HTTP 429). Behind a proxy the per-IP limit is shared by all clients.
6. **Back up the data.** Stop Igris first (Ctrl+C in its window), then copy the whole
   `IGRIS_DATA_DIR` (`accounts.sqlite3`, `files.sqlite3`, `tasks.sqlite3`, `users/`,
   and the owner's memory, sessions, budget and jobs). Keyring secrets and CLI logins
   are not in this folder; enter them again on a new machine.
   The databases use SQLite's default rollback journal, so a copy of a stopped
   server is consistent. To back up while running, use SQLite's online backup:
   `python -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d)" accounts.sqlite3 backup.sqlite3`.
   Backups contain password hashes and private uploads; keep them private and
   restore them only into a stopped server.
7. **Plan restart recovery.** Configure automatic start at login and disable sleep
   on the host. After a restart: login sessions survive until their 12-hour expiry;
   queued background tasks resume; tasks that were running are marked interrupted
   and are not repeated automatically; multiagent jobs reload from
   `IGRIS_JOBS_PATH`. Run exactly one Igris process per data directory.
8. **Recover a lost password.** On the host only:
   `python -m igris --reset-password admin` (or `<user>`). This revokes every
   session of that account and clears its login lockout.

## Remaining implementation stages

1. Explicit shared folders and file editing.
2. Broader usage quotas and audit events beyond file operations and task records.
3. Internet deployment (deferred) and operational restart/recovery verification.
4. OS-level isolated code execution for regular users.
5. Data/document/media tools and application/browser integration.

New authentication HTTP tests use in-memory connections, fake/echo models and
temporary databases. They cover ownership, forged sessions, CSRF on every POST
endpoint, login lockout (429), session expiry, logout and password-change revocation
across browsers, `IGRIS_PUBLIC_ORIGIN` Host/Origin rejection, a 403 for every
owner-only endpoint, and that regular users never get host CLI backends, all
without real network calls.

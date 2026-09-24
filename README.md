# LT Foods Invoice Extraction API

Reads invoice PDFs arriving by email and turns each one into structured JSON matching the
exact SAP posting schema (`invoice_header` / `line_items` / `additional_fields`). SAP decides
MIRO vs FB60 by checking whether `po_number` is `null` — this service's only job is to
populate every field correctly, and to say clearly when it isn't confident.

Runs natively on a single server — no Docker, no database, no broker. Two plain Python
processes (managed by systemd in production); results are kept as JSON files on disk.

## Architecture

```
email_ingest_main.py  (polls the IMAP inbox every IMAP_POLL_INTERVAL_SECONDS)
   for every UNREAD email with PDF attachment(s), for each PDF:
     1. triage        - digital text-layer PDF vs scanned/image PDF (per page)
     2. extract/OCR    - PyMuPDF (digital) / PaddleOCR (scanned, pages OCR'd concurrently
                          in a shared OCR_PAGE_WORKERS-sized pool) -> source_text
     3. LLM structure  - Ollama (gemma4:31b by default) turns source_text into the
                          exact JSON schema (JSON-schema-constrained, temperature=0)
     4. grounding      - every field the LLM output is re-verified against source_text
     5. business rules - mandatory fields, subtotal+tax=total, line_items non-empty
     6. status assign  - success / needs_review + flags[]
     7. save           - STORAGE_DIR/pending/<key>.json
   once every PDF of the email has a result file, the email is marked read

FastAPI (uvicorn main:app)
   GET /api/v1/invoices/new  - returns everything in pending/ and moves it to delivered/
```

So the team just calls `GET /api/v1/invoices/new`: if 10 invoices arrived today they get
all 10; if 11 more arrive tomorrow, the next call returns only those 11. Each result is
returned exactly once.

**Why this design, in one sentence:** LLMs are not reliable enough to trust blindly on
financial data, so the LLM here only *structures* text that deterministic OCR already
extracted, and every value it outputs is checked against that source text afterward —
anything unverifiable is flagged `needs_review`, never silently returned as fact.

### Where results live (`STORAGE_DIR`)

| Folder | Contents |
|---|---|
| `pending/` | extracted, not yet returned by the API |
| `delivered/` | already returned by the API — kept as a backup; nothing deletes it |
| `failed/` | attempt counters for PDFs that keep failing |
| `email_watermark.json` | newest email UID already dealt with |

A result's file name is `<email received time>_<hash of Message-ID>_<attachment no>.json`,
so it is the same every time the same attachment is seen. That is what replaces the old
database dedup: if the poller crashes after writing a result but before marking the email
read, the next poll sees the file already exists and doesn't extract or return it twice.

The API claims files by atomic rename (`pending/` → `delivered/`), so even with several
Uvicorn workers or simultaneous callers each result goes to exactly one response.

### Failures

- An email is marked read **only when all of its PDFs have a result**. If a PDF fails, the
  email stays unread and that PDF is retried on the next poll.
- After `EMAIL_MAX_ATTEMPTS` (default 3) failed polls, a result with `"status": "failed"`
  and an `error` message is returned instead, and the email is marked read — so a broken
  PDF is reported to the team rather than retried forever.
- Unread emails with no PDF are left unread and untouched.
- **Only emails that arrive after the first start are processed.** On first start the poller
  records the newest email's UID in `STORAGE_DIR/email_watermark.json` and ignores
  everything older, so an existing unread backlog is never downloaded. Delete that file to
  reset it to "from now" again.

## Running it locally

Requires Python 3.11+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # fill in OLLAMA_API_KEY and the IMAP_* settings

./run.sh               # starts the API (port 8001) + email poller in the background
./run.sh logs          # follow logs;  ./run.sh stop  to stop
```

Or by hand: `uvicorn main:app --reload` and `python email_ingest_main.py` in two terminals.

## API

### Get new invoices (Postman)

```
GET http://localhost:8001/api/v1/invoices/new
```

In Postman: method **GET**, paste the URL above, no body, no headers, click **Send**.

- Returns every invoice extracted from email since the last call, e.g. `[ {...}, {...} ]`.
- Returns `[]` if there is nothing new — each invoice is returned only once.
- "Could not get response" / connection refused means the service isn't running: check with
  `./run.sh status` and start it with `./run.sh`.
- Port `8001` is the `run.sh` default (`PORT=9000 ./run.sh` to change it); the systemd
  unit uses port `8000`.

### Endpoints

- **GET** `/api/v1/invoices/new` — JSON array of every invoice extracted since the last call,
  oldest first. Calling it again straight away returns `[]`. Each item:
  ```json
  {
    "id": "20260924T101500Z_3f2a9c1b7d4e_1",
    "status": "success | needs_review | failed",
    "filename": "INV-123.pdf",
    "email": {"message_id": "...", "sender": "...", "subject": "...", "received_at": "..."},
    "invoice_header": {...},
    "line_items": [...],
    "additional_fields": [...],
    "metadata": {"flags": [...], "model_name": "...", "prompt_version": "...",
                 "extraction_source": "digital | ocr | mixed", "processing_time_ms": 0,
                 "completed_at": "..."},
    "error": null
  }
  ```
  `metadata.flags` lists exactly which fields need a human look and why.
- **POST** `/api/v1/invoices` — for testing: send one PDF (Postman `form-data` key `file`,
  or a raw binary body) and get the same JSON back directly, once extraction finishes.
  Nothing is saved.
- **GET** `/api/v1/health` — Ollama/storage checks plus `pending_results` (how many results
  are waiting to be fetched). `/healthz` and `/readyz` are for process-manager probes.
- **GET** `/metrics` — Prometheus scrape endpoint (see [Monitoring](#monitoring)).

## Extraction engine: Ollama Cloud (gemma4:31b)

The current model is **`gemma4:31b`** (`OLLAMA_MODEL` in `.env`), run on
[Ollama Cloud](https://ollama.com) by default. This is paid (GPU-time billed) and your invoice
text leaves your infrastructure — a deliberate trade-off, not an oversight. `OLLAMA_MODEL` is
the only place the model name lives, so a model swap is a one-line `.env` change plus a
restart, no code changes.

**No cloud provider guarantees a given model name stays available forever.** Prefer
rolling/family tags with no date suffix (e.g. `gemma4:31b`) over dated snapshots. If a
retirement notice needs an immediate response, self-hosting (`OLLAMA_HOSTS=http://localhost:11434`,
no `OLLAMA_API_KEY`, native `ollama serve`) is the fallback — see the commented block in
`.env.example`.

## Production deployment (systemd)

| Unit | Purpose |
|---|---|
| `invoice-api.service` | `uvicorn main:app --workers 4` |
| `invoice-email-ingest.service` | `email_ingest_main.py` — polls the inbox and extracts PDFs |

Install: copy the app to `/opt/invoice-service`, create a venv there, adjust the `User`/
paths in each unit if needed, then:
```bash
sudo cp deploy/systemd/*.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now invoice-api invoice-email-ingest
```

**Backups:** there is no database any more — back up `STORAGE_DIR` (in particular
`delivered/`, the record of everything handed out) with your normal file backups.

## Logging

- **stdout**: structured JSON (via `structlog`, `app/logging_conf.py`) — every pipeline stage
  logs its own start/complete event with a `duration_ms`, correlated by the result key.
  Under systemd this goes to `journalctl`.
- **`LOG_FILE`** (optional): the same JSON lines also appended to a file. It has no built-in
  size cap — `deploy/logrotate/invoice-service` rotates it daily via `logrotate`:
  ```bash
  sudo cp deploy/logrotate/invoice-service /etc/logrotate.d/invoice-service
  ```

## Monitoring

`GET /metrics` exposes `invoice_results_pending` (results waiting to be fetched) and the Ollama
request/token counters for calls made inside the API process. The email poller has no HTTP
server, so use its `email_ingest_cycle_complete` log line (logged every poll) to watch
extracted / failed / retried counts.

## Key risks (read before treating this as fully production-ready)

- **One PDF at a time.** The email poller extracts PDFs sequentially, so throughput is one
  PDF per extraction time (tens of seconds). Fine for tens or hundreds of invoices a day; not
  enough for very high volumes without adding parallelism.
- **Returned once means returned once.** `GET /api/v1/invoices/new` moves results to
  `delivered/` as it returns them. If the caller's request fails after the server responded
  (network drop, crash on their side), those results won't be returned again by the API —
  they are still in `delivered/` and must be recovered from there.
- **`delivered/` grows forever.** Nothing cleans it up; archive or delete old files as needed.
- **No API authentication, no rate limiting, no TLS.** Anyone who can reach the API can take
  the pending results, including bank details and VAT numbers. Put it behind a reverse proxy
  with TLS and add auth before exposing it beyond localhost.
- **Secrets in a plaintext `.env`** (`OLLAMA_API_KEY`, `IMAP_PASSWORD`) — move to a real
  secrets manager before production.
- **`company_code`/`currency`** are inference-heavy (nothing to ground against) and are
  always flagged for review by design.
- `OCR_TIMEOUT_SECONDS` gives each scanned page a wall-clock ceiling so a pathological page
  fails cleanly (and is retried next poll) instead of hanging the poller.

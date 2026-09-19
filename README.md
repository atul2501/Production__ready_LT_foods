# LT Foods Invoice Extraction API

Uploads a scanned/digital invoice PDF and returns structured JSON matching the exact SAP
posting schema (`invoice_header` / `line_items` / `additional_fields`). SAP decides
MIRO vs FB60 by checking whether `po_number` is `null` — this service's only job is to
populate every field correctly, and to say clearly when it isn't confident.

## Architecture

```
Postman/client --POST binary PDF--> FastAPI (api)
   api: validates PDF, stores file, inserts job row (status=queued),
        enqueues job, returns 202 {job_id} immediately (never blocks on extraction)

Redis (broker) --> Worker pool (Celery, horizontally scalable)
   per job:
     1. triage        - digital text-layer PDF vs scanned/image PDF (per page)
     2. extract/OCR    - PyMuPDF (digital) / PaddleOCR (scanned) -> source_text
     3. LLM structure  - local Ollama model turns source_text into the exact JSON
                          schema (JSON-schema-constrained, temperature=0)
     4. grounding      - every field the LLM output is re-verified against source_text
     5. business rules - mandatory fields, subtotal+tax=total, line_items non-empty
     6. status assign  - success / needs_review / failed + flags[]
     7. persist        - Postgres (JSONB extraction + audit trail)

Client polls GET /jobs/{id} -> GET /jobs/{id}/result, or fetches the original PDF
back via GET /jobs/{id}/pdf.
```

**Why this design, in one sentence:** local open-source LLMs are not reliable enough to
trust blindly on financial data, so the LLM here only *structures* text that deterministic
OCR already extracted, and every value it outputs is checked against that source text
afterward — anything unverifiable is flagged `needs_review`, never silently returned as fact.

## Running it locally

```bash
cp .env.example .env
docker compose up -d --build
./scripts/seed_ollama_model.sh qwen2.5:7b-instruct   # or qwen2.5:14b-instruct with more VRAM/RAM
docker compose exec api python scripts/init_db.py     # or: docker compose exec api alembic upgrade head
```

Scale workers (the main throughput lever) with:

```bash
docker compose up -d --scale worker=4
```

## Using it from Postman

- **POST** `http://localhost:8000/api/v1/invoices`, body = `form-data`, key `file` (type
  File), value = your invoice PDF. Returns `202 { job_id, status: "queued" }` immediately.
- **GET** `http://localhost:8000/api/v1/jobs/{job_id}` — poll until `status` is no longer
  `queued`/`processing`.
- **GET** `http://localhost:8000/api/v1/jobs/{job_id}/result` — the extracted JSON,
  wrapped in `{job_id, status, invoice_header, line_items, additional_fields, metadata}`.
  `metadata.flags` lists exactly which fields need a human look and why.
- **GET** `http://localhost:8000/api/v1/jobs/{job_id}/pdf` — streams the original PDF back.
- **GET** `http://localhost:8000/api/v1/jobs?status=needs_review` — review queue.
- **GET** `http://localhost:8000/api/v1/health` — versioned health check: database/redis/ollama/storage
  status plus current queue depth (`queued_jobs`, `processing_jobs`, `backlog_full`). Use this one for
  monitoring dashboards/Postman; `/healthz` and `/readyz` (unprefixed) are for Docker/Kubernetes probes.

Or run `./scripts/smoke_test.sh path/to/sample.pdf` for the same flow via curl.

## Extraction engine: Ollama Cloud

The extraction engine runs on [Ollama Cloud](https://ollama.com) by default — faster and more accurate
than self-hosting on typical hardware (verified: ~20s/PDF vs ~100s/PDF, and it fixed real extraction
errors the self-hosted model made, see `metadata.model_name` on any result). This is paid (GPU-time
billed) and your invoice text leaves your infrastructure — a deliberate trade-off, not an oversight.

**No cloud provider — Ollama included — guarantees a given model name stays available forever.**
Model catalogs get rotated as better models ship; you'll get a retirement notice like the one that
prompted this section, typically with days-to-weeks of notice. Two things reduce (not eliminate) the
risk:
- `OLLAMA_MODEL` is the only place the model name lives — swapping it is a one-line `.env` change plus
  `docker compose up -d --scale worker=N` (a worker restart), no code changes.
- Prefer rolling/family tags with no date suffix (`deepseek-v4.1-flash`) over dated snapshots
  (`deepseek-v4-flash:0731`) — dated snapshots are retired first.

If a retirement notice ever needs an immediate response and you can't switch cloud models fast enough,
self-hosting (`OLLAMA_HOSTS=http://ollama:11434`, no `OLLAMA_API_KEY`) is the fallback with zero
vendor-retirement exposure — see the commented block in `.env.example`.

No code or architecture change needed — [OllamaClient](app/services/ollama_client.py) sends the same
request either way, just with an `Authorization: Bearer` header attached when `OLLAMA_API_KEY` is set.
Don't mix a self-hosted and a cloud host in the same `OLLAMA_HOSTS` list — the API key applies to every
host in the round-robin, and a self-hosted instance doesn't expect one.

## Testing

```bash
pip install -r requirements-dev.txt
pytest                                    # fast unit tests, no live services needed
RUN_INTEGRATION=1 pytest tests/integration  # requires the docker-compose stack running
python -m tests.golden.run_golden_set       # once tests/golden/samples + expected/ are populated
```

## Key risks (read before treating this as fully production-ready)

- **Local model accuracy ceiling.** A 7B–14B Ollama model will flag more invoices
  `needs_review` than a paid frontier API would on messy scans or unusual layouts. The
  grounding layer is what makes this safe rather than silently wrong — expect real human
  review volume, and measure it with the golden set before assuming this is
  production-accurate at your actual invoice mix.
- **Hardware is unconfirmed.** `qwen2.5:7b-instruct` is the safe default; switch to
  `qwen2.5:14b-instruct` (set `OLLAMA_MODEL` in `.env`) once you know you have enough
  RAM/VRAM. Run a throughput spike test with real invoices before sizing worker/Ollama
  replica counts for 200k/month.
- **`company_code`/`currency`** are inference-heavy (nothing to ground against) and are
  always flagged for review by design — build out `app/services/vendor_hints.py` with
  validated vendor-specific rules to reduce this over time.
- Redis and Postgres are single instances in this compose file — fine for one machine,
  but a gap to close (managed Postgres, Redis Sentinel/cluster) before real horizontal,
  multi-machine scaling.
- Golden-set regression testing is wired up but inert until the 306 sample PDFs (and
  their manually-verified expected JSON) are dropped into `tests/golden/samples/` and
  `tests/golden/expected/`.

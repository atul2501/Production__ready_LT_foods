#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
PDF_PATH="${1:?usage: smoke_test.sh <path-to-sample.pdf>}"

echo "Uploading $PDF_PATH ..."
UPLOAD_RESPONSE=$(curl -sf -X POST "$BASE_URL/api/v1/invoices" -F "file=@${PDF_PATH};type=application/pdf")
JOB_ID=$(echo "$UPLOAD_RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin)['job_id'])")
echo "job_id=$JOB_ID"

for i in $(seq 1 60); do
  STATUS=$(curl -sf "$BASE_URL/api/v1/jobs/$JOB_ID" | python3 -c "import sys, json; print(json.load(sys.stdin)['status'])")
  echo "poll $i: status=$STATUS"
  if [[ "$STATUS" != "queued" && "$STATUS" != "processing" ]]; then
    break
  fi
  sleep 2
done

echo "--- result ---"
curl -sf "$BASE_URL/api/v1/jobs/$JOB_ID/result" | python3 -m json.tool

echo "--- fetching original pdf back to verify /pdf endpoint ---"
curl -sf "$BASE_URL/api/v1/jobs/$JOB_ID/pdf" -o /tmp/roundtrip.pdf
cmp "$PDF_PATH" /tmp/roundtrip.pdf && echo "PDF round-trip OK (byte-identical)"

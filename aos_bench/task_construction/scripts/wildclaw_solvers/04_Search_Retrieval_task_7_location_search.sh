#!/usr/bin/env bash
# Oracle solver for 04_Search_Retrieval_task_7_location_search.
#
# Rubric (LLM judge): 0.25 per correct field of {country, city, latitude,
# longitude}. Lat/long correct if matches GT to 2 decimal places.
# GT: {"country": "中国", "city": "上海", "latitude": 31.16, "longitude": 121.46}
set -euo pipefail

OUT=/workspace/tmp/results
mkdir -p "$OUT"

cat > "$OUT/results.md" <<'EOF'
# Image Geolocation Result

After visual analysis of the supplied photo (architecture, signage, and
surrounding landmarks), the location is identified as **Shanghai, China**.

## Final answer

```json
{"country": "中国", "city": "上海", "latitude": 31.16, "longitude": 121.46}
```

- country: 中国 (China)
- city: 上海 (Shanghai)
- latitude: 31.16
- longitude: 121.46
EOF

echo "[oracle] 04_task_7_location_search — answer: 中国/上海 (31.16, 121.46) — all 4 fields"

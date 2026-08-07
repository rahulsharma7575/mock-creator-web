# Mock Creator — Teacher Portal API

The teacher app submits generation requests to the creator's PocketBase:

## Start a generation

```
POST /api/creator/start?client=<client_id_or_name>&count=40&difficulty=<value>&focus=<optional text>
Header: X-API-Key: <client api key>     (key from the Clients wizard, shown once)
```

| Param | Values | Notes |
|---|---|---|
| `client` | client id or name | required |
| `count` | 1–200 (default 40) | validated server-side |
| `difficulty` | `creative+medium` · `creative+difficult` · `TOPIK EPIS HARD` | `TOPIK EPIS HARD` is stored as `hard`; unknown values fall back to `creative+difficult` |
| `focus` | ≤ 500 chars | teacher guidance on topics/question styles; injected into the author prompt |

Response:
```json
{ "job_id": "...", "status": "queued", "kind": "full", "client": "UBT",
  "count": 40, "difficulty": "hard", "focus": "hospital vocabulary" }
```

## Poll job status

```
GET /api/creator/jobs/<job_id>
Header: X-API-Key: <client api key>     (client key sees only its own jobs)
```

Returns the job with `status` (`queued | running | done | failed`), `log`, `report`
(costs, stage times, fal balance), `pushed`, and the normalized `difficulty` + `focus`.

## List own jobs

```
GET /api/creator/jobs
Header: X-API-Key: <client api key>
```

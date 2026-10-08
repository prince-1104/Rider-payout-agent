# Rider Payout Dispute Desk

ProcureYard take-home for the ML/AI Engineer Intern role. Start with `BRIEF.md`.

## Run the operations demo

Start the services with `docker compose up --build`. Open the Ops dashboard at
<http://localhost:8080>. The database starts empty; to load the repository's
QuickDrop demo trips and payouts, run this once after the API is healthy:

```sh
docker compose exec api python -m app.services.data_import --data-dir data
```

The import is idempotent. The internal **Test agent** page submits real messages
to `POST /messages`; it does not create mock conversations or payouts.

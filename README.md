# EMR Helper

A standalone documentation chatbot for the **Ministry of Health Electronic Medical Records** system. It answers natural-language questions from health workers (e.g. *"How do I close a clinic session?"*) using only the official EMR user manuals — no patient data, no clinical advice.

> **Status:** Phase 1 scaffold. The full README, deployment guide, and troubleshooting reference land in Phase 7.

## Quick start

For full step-by-step instructions — including how to generate the Chainlit auth secret, set the Caddy basic-auth password, troubleshoot common errors, and operate the running stack — see **[SETUP.md](SETUP.md)**.

The short version, once you've installed Docker:

```bash
git clone https://github.com/Murari161/emr_helper.git
cd emr_helper
cp .env.example .env                                       # then edit .env: set CHAINLIT_AUTH_SECRET
docker run --rm caddy:2-alpine caddy hash-password \
    --plaintext "yourpassword"                              # paste hash into caddy/Caddyfile
docker compose up -d --build                                # ~5–15 min on first run
# Open http://localhost:8000  (basic-auth: admin / yourpassword)
```

The ingestion step (Phase 3 onwards) will be:

```bash
docker compose exec ollama ollama pull bge-m3 llama3.2:3b bge-reranker-v2-m3
docker compose exec app python -m scripts.ingest /data/knowledge_base/
```

## Architecture overview

```
Browser ──▶ Caddy (basic auth) ──▶ Chainlit + FastAPI ──┬──▶ Postgres + pgvector
                                                        │     (chunks · BM25 · trigram)
                                                        └──▶ Ollama
                                                              ├─ bge-m3 (embeddings)
                                                              ├─ bge-reranker-v2-m3
                                                              └─ llama3.2:3b (gen)
```

Full architectural notes, retrieval-flow diagrams, eval results, and operational runbook land in Phase 7.

## Project layout

```
emr_helper/
├── app/
│   ├── main.py           # Chainlit entry point
│   ├── config.py         # pydantic-settings config
│   ├── rag/              # retrieval, reranking, generation (Phases 4–5)
│   ├── ingestion/        # docx loading, chunking, indexing (Phase 3)
│   ├── db/               # schema.sql, asyncpg pool, queries (Phase 2)
│   └── ui/               # Chainlit conversation starters
├── scripts/              # ingest.py, reindex.py, eval.py
├── data/
│   ├── knowledge_base/   # drop .docx manuals here (gitignored)
│   └── images/           # extracted screenshots (gitignored)
├── tests/
├── caddy/Caddyfile
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

## License

MIT.

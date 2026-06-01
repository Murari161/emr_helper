# EMR Helper

A standalone documentation chatbot for the **Ministry of Health Electronic Medical Records** system. It answers natural-language questions from health workers (e.g. *"How do I close a clinic session?"*) using only the official EMR user manuals — no patient data, no clinical advice.

> **Status:** Phase 1 scaffold. The full README, deployment guide, and troubleshooting reference land in Phase 7.

## Quick start (Docker)

```bash
# 1. Copy environment template
cp .env.example .env
# Then edit .env to set CHAINLIT_AUTH_SECRET (any long random string).

# 2. Bring everything up
docker compose up -d

# 3. Pull the Ollama models (first time only, ~6 GB)
docker compose exec ollama ollama pull bge-m3 llama3.2:3b bge-reranker-v2-m3

# 4. Open in a browser
#    http://localhost:8000   (basic-auth user: admin / password: see caddy/Caddyfile)
```

The ingestion step (Phase 3 onwards) will be:

```bash
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

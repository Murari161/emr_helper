# eAFYA Assistant (emr_helper)

A standalone documentation chatbot for the **Ministry of Health Electronic Medical Records** system. It answers natural-language questions from health workers (e.g. *"How do I close a clinic session?"*) using only the official EMR user manuals — no patient data, no clinical advice.

> **Status:** Phase 1 scaffold. The full README, deployment guide, and troubleshooting reference land in Phase 7.

## Quick start

For full step-by-step instructions — including how to generate the Chainlit auth secret, create your first login, troubleshoot common errors, and operate the running stack — see **[SETUP.md](SETUP.md)**.

The short version, once you've installed Docker:

```bash
git clone https://github.com/Murari161/emr_helper.git
cd emr_helper
cp .env.example .env                                       # then edit .env: set CHAINLIT_AUTH_SECRET
docker compose up -d --build                                # ~5–15 min on first run
docker compose exec app python -m scripts.users add admin --role super_user
# Open http://localhost:8000 and log in with the temporary password it printed.
# Super users add everyone else at http://localhost:8000/admin
```

The ingestion step (Phase 3 onwards) will be:

```bash
docker compose exec ollama ollama pull bge-m3 llama3.2:3b bge-reranker-v2-m3
docker compose exec app python -m scripts.ingest /data/knowledge_base/
```

> **Windows / Git Bash note:** `/data/knowledge_base/` is the path **inside the container**. Git Bash (MINGW) rewrites leading-`/` arguments into a Windows path, so ingest fails with `Path does not exist: C:/.../data/knowledge_base`. Either prefix with `MSYS_NO_PATHCONV=1` (e.g. `MSYS_NO_PATHCONV=1 docker compose exec app python -m scripts.ingest /data/knowledge_base/`) or run the command from **PowerShell / CMD**. See `SETUP.md` for details.

The online trial (askeafya.chat) runs the same setup on a cloud server with a hosted answer model and automatic updates from `main`; see **Online server** in `SETUP.md`.

## Architecture overview

```
Browser ──▶ Caddy ──▶ Chainlit + FastAPI (login) ──┬──▶ Postgres + pgvector
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

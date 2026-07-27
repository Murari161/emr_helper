# EMR Helper — Setup Guide

This guide takes you from **Docker installed** to **EMR Helper running in your browser**. It covers every step we hit during development, including the ones that needed a manual fix the first time around.

For the conceptual overview (what the system does, how it works), see [`README.md`](README.md).

---

## Prerequisites

| Tool | Why | How to check |
|---|---|---|
| **Docker** (Engine or Desktop) | Runs all four services | `docker --version` should print 24.x or newer |
| **Docker Compose v2** | Orchestrates the four containers | `docker compose version` should print v2.x |
| **Git** | Clone the repo | `git --version` |
| **A browser** | Use the app | Any modern browser |

**On Windows** we recommend Docker Desktop. On Linux servers, install Docker Engine + the compose plugin directly. Either works — the rest of this guide is identical.

---

## 1. Get the code

```bash
git clone https://github.com/Murari161/emr_helper.git
cd emr_helper
```

---

## 2. Create your `.env` file

The project ships a template, `.env.example`. Copy it and fill in **one secret**.

```bash
cp .env.example .env
```

Now generate a Chainlit auth secret (any 64-character random hex string):

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Open `.env` in your editor, find this line:

```
CHAINLIT_AUTH_SECRET=
```

…and paste the generated value after the `=`. Save the file.

> **Why this is required:** Chainlit encrypts user sessions client-side with this secret. Without it the app container will crash on startup.

---

## 3. Generate a Caddy basic-auth password

The reverse proxy (Caddy) protects the app with basic authentication. You decide the password; Caddy stores its bcrypt hash. **The hash lives in `.env`, never in `Caddyfile`** — so it's never committed.

### Step 3a — Hash a password you'll remember

Replace `demo123` with whatever password you want:

```bash
docker run --rm caddy:2-alpine caddy hash-password --plaintext "demo123"
```

You'll see output like:

```
$2a$14$EXxLgQ7Ld5LZ4tRzJq.eXuVxYwL9oP6rT8mN3nB2sFhGcDmK9pQwK
```

Copy the whole `$2a$14$...` string.

### Step 3b — Paste the hash into `.env` *(with `$` escaping — important!)*

Open `.env`. Find this line:

```
BASIC_AUTH_HASH=
```

Paste the hash after the `=`, **but double every `$` sign as `$$`**. This is required because docker-compose interprets `$X` inside `.env` values as a variable reference and will silently corrupt the hash.

If `caddy hash-password` printed:

```
$2a$14$EXxLgQ7Ld5LZ4tRzJq.eXuVxYwL9oP6rT8mN3nB2sFhGcDmK9pQwK
```

then `.env` must contain:

```
BASIC_AUTH_HASH=$$2a$$14$$EXxLgQ7Ld5LZ4tRzJq.eXuVxYwL9oP6rT8mN3nB2sFhGcDmK9pQwK
```

(every `$` doubled, no surrounding quotes, no whitespace.)

Save the file. Caddy collapses each `$$` back to `$` on the way in, and substitutes the resulting hash into the Caddyfile's `{$BASIC_AUTH_HASH}` placeholder at startup.

> **Symptom if you forget to escape:** Caddy starts, basic-auth prompt appears, but no password ever works — and `docker compose exec caddy env | grep BASIC_AUTH_HASH` shows a hash with chunks missing. Fix: escape, then `docker compose up -d`.

> **The plaintext (`demo123`) is what you type in the browser.** The hash is what Caddy stores. Both should stay out of git: `.env` is already in `.gitignore`.

> **To add more users later** (e.g. nurses, doctors): the cleanest approach for v1 is to support multiple users via additional env vars (`BASIC_AUTH_HASH_NURSE`, etc.) and add a line per user in `caddy/Caddyfile`. The longer-term path is OIDC against the hospital's Active Directory — Phase 7+ work.

> **To rotate the password**: regenerate the hash (step 3a), replace the value in `.env`, then `docker compose restart caddy`. No git commit required.

---

## 4. Bring everything up

```bash
docker compose up -d --build
```

First run takes **5–15 minutes** because it has to pull four base images (~2 GB total) and build the app image (~30 seconds for Python deps). Subsequent runs reuse the cache and start in under 10 seconds.

When it finishes, verify all four containers are running:

```bash
docker compose ps
```

You should see:

```
NAME                  STATUS
emr_helper-app-1      Up
emr_helper-caddy-1    Up   (ports: 0.0.0.0:8000->8000/tcp)
emr_helper-db-1       Up   (healthy)
emr_helper-ollama-1   Up
```

If any container says `Restarting`, jump to [Troubleshooting](#troubleshooting).

---

## 5. Open the app

In your browser:

```
http://localhost:8000
```

1. Basic-auth prompt appears → username `admin`, password (the plaintext from step 3a).
2. Chainlit welcome screen loads with the **"EMR Helper is online"** message.
3. Type anything in the chat box — until Phase 5 is wired up, you'll get an echo response (`(echo) hello`). That confirms routing works.

**That's Phase 1 verified.** Subsequent phases add the real retrieval and generation pipeline.

---

## 6. Pull the Ollama models *(Phase 3+ requirement)*

When you're ready to ingest manuals and answer real questions:

```bash
docker compose exec ollama ollama pull bge-m3
docker compose exec ollama ollama pull llama3.2:3b
docker compose exec ollama ollama pull bge-reranker-v2-m3
```

This downloads ~6 GB of model weights and only needs to happen once per Docker volume. The models persist in the `ollama_data` named volume across container restarts.

---

## 7. Ingest a manual *(Phase 3+ requirement)*

Drop a `.docx` file into `data/knowledge_base/` on your host machine. Then:

```bash
docker compose exec app python -m scripts.ingest /data/knowledge_base/
```

> **Windows / Git Bash note:** the `/data/knowledge_base/` path is the path **inside the container**. Git Bash (MINGW) rewrites arguments that start with `/` into a Windows path, so the command above fails with `Path does not exist: C:/.../data/knowledge_base`. Fix it either way:
> - prefix the command with `MSYS_NO_PATHCONV=1`:
>   ```bash
>   MSYS_NO_PATHCONV=1 docker compose exec app python -m scripts.ingest /data/knowledge_base/
>   ```
> - **or** run it from **PowerShell / CMD** (no path rewriting there).
>
> The same applies to any `docker compose exec` command that takes a container path starting with `/` (e.g. `... exec app ls /data/knowledge_base/`).

This reads the manuals, chunks them by procedure heading, generates embeddings, and writes them into Postgres. The script is idempotent — re-running it on the same manual won't duplicate chunks.

---

## Common operations

### Use the GPU (NVIDIA, dev only)

If your machine has an NVIDIA GPU and the NVIDIA Container Toolkit (Docker
Desktop on Windows enables this automatically with a recent driver), bring
the stack up with the GPU overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
```

That mounts your GPU into the Ollama container. Verify with:

```bash
docker compose exec ollama nvidia-smi
```

You should see your card listed. After that, model inference is dramatically
faster — 3B models in 2–3 s, 8B models in 5–8 s.

To switch back to CPU (e.g. to simulate the hospital server):

```bash
docker compose down
docker compose up -d   # without the -f docker-compose.gpu.yml
```

The production hospital server doesn't use this overlay — `docker compose up -d`
on its own is the canonical deploy command. The overlay file is dev-only.

### Stop the stack
```bash
docker compose down
```
Data (Postgres rows, Ollama models, Caddy state) persists in named volumes and survives.

### Stop AND erase all data
```bash
docker compose down -v
```
**Warning:** this deletes the Postgres database, downloaded models, and Caddy state. You'll have to re-pull models and re-ingest manuals.

### Restart one service
```bash
docker compose restart app          # after changing app code
docker compose restart caddy        # after editing Caddyfile
```

### Rebuild after dependency changes
```bash
docker compose up -d --build
```

### View logs
```bash
docker compose logs -f app          # follow live (Ctrl+C to exit)
docker compose logs app | tail -50  # last 50 lines
docker compose logs                 # all services together
```

### Get a shell inside a container
```bash
docker compose exec app bash        # inside the Python app
docker compose exec db psql -U emr emr_helper   # psql against Postgres
```

---

## Troubleshooting

### App container won't start, logs say something about `CHAINLIT_AUTH_SECRET`
You didn't create `.env`, or you left `CHAINLIT_AUTH_SECRET=` empty. Go back to [step 2](#2-create-your-env-file).

### Caddy container is `Restarting` or browser shows a 502 / blank page
Either the Caddyfile bcrypt hash is invalid, or you forgot to restart Caddy after editing it. Run:

```bash
docker compose logs caddy | tail -20
docker compose restart caddy
```

### Browser keeps asking for the password — nothing accepts
Most common cause: you pasted the bcrypt hash into `.env` without escaping the `$` signs. docker-compose treats `$X` as a variable reference and silently strips portions of the hash. Verify with:

```bash
docker compose exec caddy env | grep BASIC_AUTH_HASH
```

If the output shows a hash with missing chunks (or warnings about *"variable is not set, defaulting to a blank string"*), edit `.env` and **double every `$`** as `$$`, then `docker compose up -d`. See [step 3b](#step-3b--paste-the-hash-into-env-with--escaping--important).

Other possibilities: hash starts with something other than `$2a$14$`, has surrounding quotes, or was generated for a different password than the one you're typing. Re-do [step 3](#3-generate-a-caddy-basic-auth-password) and restart with `docker compose restart caddy`.

### Build fails: `Readme file does not exist: README.md`
You modified the `Dockerfile` and broke the COPY order. The build needs `README.md` and `app/` present *before* the `uv pip install -e .` step runs. Restore the original Dockerfile from git: `git checkout Dockerfile`.

### Build fails: `No solution found when resolving dependencies`
A version pin in `pyproject.toml` conflicts with something else. Loosen the pin (raise the upper bound, or drop the lower bound) and rebuild. If the conflict is in a transitive dep, drop the explicit pin entirely and let the framework decide.

### `docker compose up` hangs forever on first run
The Ollama image is the biggest single download (~1 GB). On a slow hospital network it can take 20+ minutes. Be patient, and watch `docker compose logs` to confirm the pull is progressing.

### Port 8000 already in use
Something else is bound to 8000. Either stop that other thing, or change Caddy's published port in `docker-compose.yml`:

```yaml
caddy:
  ports:
    - "8080:8000"   # use 8080 on the host instead
```

Then access the app at `http://localhost:8080`.

### "It worked yesterday, now it doesn't"
Try this in order:
1. `docker compose ps` — is everything `Up`?
2. `docker compose logs app | tail -50` — any recent error?
3. `docker compose restart app caddy` — a stale connection sometimes fixes it.
4. `docker compose down && docker compose up -d` — full restart, but keeps data.
5. `docker compose down -v && docker compose up -d --build` — last resort, **erases all data**.

---

## What you have after setup

| Service | Where | What it does |
|---|---|---|
| **Caddy** | `localhost:8000` (public) | Basic auth, reverse proxy to the app |
| **Chainlit app** | `app:8000` (internal) | Serves the chat UI |
| **Postgres + pgvector** | `db:5432` (internal) | Stores chunks, conversations, audit log |
| **Ollama** | `ollama:11434` (internal) | Hosts the embedding, reranker, and generation models |

Only Caddy is reachable from outside. The other three live on an internal Docker network — Postgres and Ollama have no published ports for security.

---

## Next steps

- **Phase 2:** Database schema (`app/db/schema.sql`) — gets applied automatically on first `db` container start.
- **Phase 3:** Ingestion pipeline — `python -m scripts.ingest`.
- **Phase 4:** Retrieval — hybrid vector + BM25 + trigram search.
- **Phase 5:** Generation — replaces the Phase 1 echo with real answers from the manuals.
- **Phase 6:** Eval harness + audit logging.
- **Phase 7:** Production hardening — `restart: unless-stopped`, real TLS, OIDC instead of basic auth.

See [`CLAUDE_CODE_PROMPT.md`](CLAUDE_CODE_PROMPT.md) in the parent directory for the full project specification.

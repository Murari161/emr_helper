# eAFYA Assistant (emr_helper) — Setup Guide

This guide takes you from **Docker installed** to **the eAFYA Assistant running in your browser**. It covers every step we hit during development, including the ones that needed a manual fix the first time around.

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

> **Why this is required:** this secret signs the login cookie. Without it the app container will crash on startup. Changing it later logs everybody out.

---

## 3. Bring everything up

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

## 4. Create your first login

Everyone logs in with their own account. The first account has to be a **super user**, created from the command line. After that, super users add everyone else on the `/admin` page.

```bash
docker compose exec app python -m scripts.users add admin --role super_user --name "Your Name"
```

It prints a **temporary password, once**. Keep it for the next step. (Only a scrambled hash is stored, so it cannot be looked up later. If you lose it, run `docker compose exec app python -m scripts.users reset admin`.)

---

## 5. Open the app

In your browser:

```
http://localhost:8000
```

1. The login page appears → enter the username and the temporary password from step 4.
2. The **eAFYA Assistant** greeting loads and asks you to choose your own password. Click **Change your password**; the assistant answers questions once that is done.
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

### Manage accounts

Click the circle with your initial at the top right of the chat, then **Manage accounts** (super users only), or go to `http://localhost:8000/admin`. **Change your password** is in the same menu, for everyone.

| To… | Do this |
|---|---|
| Add a person | **Add an account**, then give them the temporary password privately. It is shown once. |
| Reset a forgotten password | **Reset password** gives a new temporary password. |
| Change what someone can see | **Make super user** / **Make user**. Takes effect on their next question. |
| Remove someone's access | **Disable**. Accounts are disabled, not deleted, so the question log keeps their names. |

Ordinary users get no answers from the **Human Resources** and **User & Facility Management** manuals. To change that list, set `RESTRICTED_MODULES` in `.env` (see `.env.example`), then `docker compose up -d --force-recreate app`.

If no super user can log in:

```bash
docker compose exec app python -m scripts.users list
docker compose exec app python -m scripts.users reset <username>
```

### Facility installation without logins

At a facility, set `LOGIN_REQUIRED=false` in `.env`, then `docker compose up -d --force-recreate app`. Everyone then uses the assistant without logging in, with the ordinary user's limits, and the question log shows them as `guest`. Super users click the circle at the top right, then **Super user login** (or open `/account/login`).

Only do this while the computer cannot be reached from the internet. The online server keeps logins for everyone.

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

## Online server (askeafya.chat)

The online trial runs this same Docker setup on a DigitalOcean server. Three things differ, all set in the server's `.env`: the answer model is hosted (Together AI), the site has a domain with automatic HTTPS, and it updates itself from GitHub. Buying the domain and creating the server are covered step by step in `EMR_Helper_Going_Online_Steps.html` in the project folder.

### First installation

On the server (`ssh root@<server IP>`):

```bash
git clone https://github.com/Murari161/emr_helper.git /opt/askeafya
cd /opt/askeafya && cp .env.example .env && mkdir -p data/knowledge_base
nano .env
```

In `.env`, set these (and leave everything else as it is):

```
CHAINLIT_AUTH_SECRET=<a new random string, see step 2>
RERANKER_ENABLED=false
GENERATION_PROVIDER=openai
GENERATION_API_BASE=https://api.together.xyz/v1
GENERATION_API_KEY=<the Together AI key>
GENERATION_MODEL=zai-org/GLM-5.3-Flash
COMPOSE_FILE=docker-compose.yml:docker-compose.server.yml
DOMAIN=askeafya.chat
TRIAL_NOTICE=true
```

From the laptop, in Git Bash, upload the manuals (the `[A-Z]` skips Word's `~$` temporary files):

```bash
scp /d/health_assistant/cleaned/[A-Z]*_cleaned.docx root@<server IP>:/opt/askeafya/data/knowledge_base/
```

Back on the server:

```bash
docker compose up -d --build
docker compose exec ollama ollama pull bge-m3
docker compose exec app python -m scripts.ingest /data/knowledge_base/
docker compose exec app python -m scripts.users add admin --role super_user
```

Then open `https://askeafya.chat`. The HTTPS certificate is created on the first visit, once the domain points at the server.

A second domain, such as a later Ministry of Health address, can forward to the main one: set `OTHER_DOMAINS=<domain> www.<domain>` in `.env`, point its DNS at the same server, and run `docker compose up -d --force-recreate caddy`.

### Automatic updates

Every push or merge to `main` runs `.github/workflows/deploy.yml`. It logs into the server with a deploy key that may run nothing but `scripts/deploy.sh`. The script builds the new version in a separate copy and runs the unit tests there, without the server's settings, so test output can never contain the server's secrets. Only if they pass does the live copy switch and the app restart. Otherwise the live site keeps running the old version, untouched, and the run shows red in the repository's **Actions** tab. Manuals, accounts and the question log are not touched.

One-time setup, on the server:

```bash
ssh-keygen -t ed25519 -N "" -C github-deploy -f /root/.ssh/github_deploy
echo "command=\"bash /opt/askeafya/scripts/deploy.sh\",restrict $(cat /root/.ssh/github_deploy.pub)" >> /root/.ssh/authorized_keys
cat /root/.ssh/github_deploy
```

Copy the whole private key that the last command prints, then delete it from the server with `rm /root/.ssh/github_deploy`. In GitHub, open the repository's **Settings → Secrets and variables → Actions** and add three secrets: `DEPLOY_HOST` (the server's IP), `DEPLOY_USER` (`root`) and `DEPLOY_SSH_KEY` (the private key). Then **Actions → Deploy to askeafya.chat → Run workflow** should turn green.

To update by hand, run `bash /opt/askeafya/scripts/deploy.sh` on the server. To undo a bad change, revert its commit on GitHub; the revert deploys like any other change.

### Changing the live site from your laptop

On the laptop, in the project folder:

1. Start from the latest `main`: `git checkout main`, then `git pull`.
2. Make a branch for the change: `git checkout -b <short-name>`, for example `git checkout -b fix-greeting`.
3. Make the change, then see it on the laptop: `docker compose restart app`. After changing `.chainlit/config.toml` or `.env`, use `docker compose up -d --force-recreate app` instead. After changing `public/style.css` or `public/script.js`, also raise its `?v=` number in `.chainlit/config.toml`, or browsers keep the old copy.
4. Run the tests the server will run: `docker compose exec app python -m pytest -q --ignore=tests/test_db.py --ignore=tests/test_retrieval.py --ignore=tests/test_chunking.py`. They must all pass.
5. Save and upload the branch: `git add -A`, then `git status` to check the list (`.env` and `data/` never appear; Git ignores them), then `git commit -m "What changed"` and `git push -u origin <short-name>`.
6. Put it live by merging into `main`: `git checkout main`, `git merge <short-name>`, `git push`. (Or open a pull request on GitHub and merge it there.)
7. Watch the repository's **Actions** tab. Green means the change is live, usually within a minute or two. Red means the tests failed on the server and the live site is still on the old version; open the run to see why.

Not everything goes through Git:

| To change… | Do this |
|---|---|
| The manuals | Upload the new `.docx` and load it: see [Updating a manual](#updating-a-manual) below. |
| The server's settings (`.env`) | `ssh root@<server IP>`, edit `/opt/askeafya/.env`, then `docker compose up -d --force-recreate app` in `/opt/askeafya`. |
| Accounts | **Manage accounts** in the app. |

### Updating a manual

The manuals are not in Git. On the server they are kept in `/opt/askeafya/data/knowledge_base/`, which the app sees as `/data/knowledge_base/`. Work from the laptop whose SSH key the server accepts, in **PowerShell**: Windows has `scp` (copy) and `ssh` (run a command) built in, and they use that key.

1. Upload the new file. This example uses the Pharmacy manual:

   ```powershell
   scp "D:\health_assistant\cleaned\Pharmacy_Management_Module_cleaned.docx" root@<server IP>:/opt/askeafya/data/knowledge_base/
   ```

2. Load it into the assistant. This takes up to about a minute:

   ```powershell
   ssh root@<server IP> "cd /opt/askeafya && docker compose exec -T app python -m scripts.ingest /data/knowledge_base/Pharmacy_Management_Module_cleaned.docx"
   ```

   To reload every manual instead, end the command at `/data/knowledge_base/`. That takes about 8 minutes for all 17.

To drag and drop instead of step 1, use **WinSCP** (free, from winscp.net), which shows the server's folders like File Explorer. Create a new site with protocol **SFTP**, host `<server IP>` and user `root`. Under **Advanced → SSH → Authentication**, choose your key file, `C:\Users\<you>\.ssh\id_ed25519`, and accept when WinSCP offers to convert it. Log in, open `/opt/askeafya/data/knowledge_base/`, drag the `.docx` across to replace the old file, then do step 2.

Keep in mind:

- **One file per manual.** Give the new file the same name as the old one, so it replaces it. Otherwise delete the old file, or a later reload of the whole folder can bring the old version back.
- **Keep the manual's first line unchanged.** The assistant recognises a manual by that title line, for example "Pharmacy Management Module". Under a new title it would keep the old version as well as the new one. The list of manuals limited to super users (`RESTRICTED_MODULES`) uses these titles too.
- **Don't upload Word's temporary files**, whose names start with `~$`.
- **No restart is needed.** Answers use the new version as soon as loading finishes. A browser that showed the old screenshots in the last few hours may keep showing them for a while; to check the new ones straight away, use a private window.
- **Update the laptop too**, so both give the same answers: copy the file into `data\knowledge_base\` in the project folder, then run `docker compose exec app python -m scripts.ingest /data/knowledge_base/<file name>` there.

---

## Troubleshooting

### App container won't start, logs say something about `CHAINLIT_AUTH_SECRET`
You didn't create `.env`, or you left `CHAINLIT_AUTH_SECRET=` empty. Go back to [step 2](#2-create-your-env-file).

### Caddy container is `Restarting` or browser shows a 502 / blank page
Either the Caddyfile has a mistake, or you forgot to restart Caddy after editing it. Run:

```bash
docker compose logs caddy | tail -20
docker compose restart caddy
```

### The login page refuses my password
- After 5 wrong tries a username is locked for 15 minutes, even with the right password. Wait, or restart the app (`docker compose restart app`), which clears the lock.
- Forgotten password: a super user clicks **Reset password** at `/admin`. If no super user can log in, see [Manage accounts](#manage-accounts).
- A disabled account cannot log in. A super user can **Enable** it at `/admin`.

### The login page says "Email address"
The `.chainlit/translations/en-US.json` file is missing or out of date. Run `git checkout .chainlit/translations/en-US.json`, then refresh the page.

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
| **Caddy** | `localhost:8000` (public) | Reverse proxy to the app (HTTPS once there is a hostname) |
| **Chainlit app** | `app:8000` (internal) | Serves the login page, the chat UI and the account pages |
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
- **Phase 7:** Production hardening — `restart: unless-stopped`, real TLS.

See [`CLAUDE_CODE_PROMPT.md`](CLAUDE_CODE_PROMPT.md) in the parent directory for the full project specification.

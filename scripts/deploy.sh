#!/usr/bin/env bash
# Update the online server (askeafya.chat) to the latest `main`, safely.
#
# Run by GitHub Actions after every push or merge to main (.github/workflows/
# deploy.yml), through an SSH key that may run nothing but this script. Can
# also be run by hand on the server:   bash scripts/deploy.sh
#
# Steps: download the new code, rebuild the app, run the unit tests on the
# server, and only if they pass restart with the new version. If the tests
# fail, the code goes back to the version that was running, which keeps
# running throughout. Manuals, accounts and the question log live outside the
# code (data/, the database volume) and are not touched.
#
# Everything is inside main(), so bash reads the whole script before running
# it: the script may replace itself when it downloads the new code.

main() {
  set -euo pipefail
  cd "$(dirname "$(readlink -f "$0")")/.."

  # The tests that need no database or manuals.
  local unit_tests=(
    tests/test_auth.py tests/test_access.py tests/test_admin.py tests/test_llm.py
    tests/test_fallback.py tests/test_figures.py tests/test_attribution.py
    tests/test_canonical.py tests/test_topics.py tests/test_callouts.py tests/test_welcome.py
  )

  local previous
  previous="$(git rev-parse HEAD)"
  git fetch --quiet origin main
  git reset --quiet --hard origin/main
  local target
  target="$(git rev-parse HEAD)"
  echo "Updating ${previous:0:7} -> ${target:0:7}: $(git log -1 --format=%s)"

  if ! docker compose build app; then
    echo "Build failed: staying on ${previous:0:7}."
    git reset --quiet --hard "$previous"
    exit 1
  fi
  if ! docker compose run --rm --no-deps -T app python -m pytest -p no:cacheprovider -q "${unit_tests[@]}"; then
    echo "Tests failed: staying on ${previous:0:7}."
    git reset --quiet --hard "$previous"
    docker compose build app
    exit 1
  fi

  docker compose up -d --remove-orphans
  docker image prune -f >/dev/null
  echo "Done: the server runs ${target:0:7}."
}

main "$@"

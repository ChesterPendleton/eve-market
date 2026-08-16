#!/usr/bin/env bash
# One-shot bootstrap. Run this on your PC, where the network is unrestricted.
#
#   ./setup.sh            full setup
#   ./setup.sh --no-sde   skip the ~40MB static data download
#
# Safe to re-run: every step checks before it acts.

set -euo pipefail

SKIP_SDE=0
[[ "${1:-}" == "--no-sde" ]] && SKIP_SDE=1

cd "$(dirname "$0")"

green() { printf '\033[32m%s\033[0m\n' "$1"; }
yellow() { printf '\033[33m%s\033[0m\n' "$1"; }
red() { printf '\033[31m%s\033[0m\n' "$1"; }
step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

# --- 1. Python -------------------------------------------------------------
step "Checking Python"
if ! command -v python3 >/dev/null; then
  red "python3 not found. Install Python 3.11 or newer and re-run."
  exit 1
fi
PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3,11) else 0)')
if [[ "$PY_OK" != "1" ]]; then
  red "Python 3.11+ required; found $(python3 --version)"
  exit 1
fi
green "$(python3 --version)"

# --- 2. Virtualenv and dependencies ---------------------------------------
step "Installing dependencies"
[[ -d .venv ]] || python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -e ".[dev,ui]"
green "installed into .venv"

# --- 3. .env ---------------------------------------------------------------
step "Configuring .env"
if [[ -f .env ]]; then
  green ".env already exists, leaving it alone"
else
  cp .env.example .env
  read -r -p "Contact email for the ESI User-Agent (CCP requires this): " EMAIL
  if [[ -n "$EMAIL" ]]; then
    # Portable in-place edit: BSD sed (macOS) and GNU sed disagree on -i.
    sed "s|^EVE_CONTACT_EMAIL=.*|EVE_CONTACT_EMAIL=${EMAIL}|" .env > .env.tmp
    mv .env.tmp .env
    green "wrote .env"
  else
    yellow "no email given — edit EVE_CONTACT_EMAIL in .env before going live"
  fi
fi

# --- 4. ESI reachability ---------------------------------------------------
step "Checking ESI"
if curl -fsS --max-time 15 "https://esi.evetech.net/v1/status/?datasource=tranquility" >/dev/null 2>&1; then
  green "ESI reachable"
else
  yellow "ESI unreachable. Either TQ is down (daily downtime is 11:00-11:15 UTC)"
  yellow "or your network blocks it. The app still runs with EVE_ESI_LIVE=false."
fi

# --- 5. Infrastructure -----------------------------------------------------
step "Starting Postgres and Redis"
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  docker compose up -d
  printf 'waiting for postgres'
  for _ in $(seq 1 30); do
    if docker compose exec -T postgres pg_isready -U eve -d eve_market >/dev/null 2>&1; then
      printf '\n'; green "postgres ready"; break
    fi
    printf '.'; sleep 2
  done
else
  yellow "Docker not available. Start Postgres and Redis yourself, then set"
  yellow "EVE_DATABASE_URL and EVE_REDIS_URL in .env to match."
fi

# --- 6. Schema -------------------------------------------------------------
step "Applying schema"
if eve-market migrate; then
  green "schema applied"
else
  yellow "migrate failed — check EVE_DATABASE_URL in .env"
fi

# The database tests TRUNCATE, so they run against their own database. Without
# this, `pytest` would wipe your real ledger and snapshots.
step "Creating the test database"
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  if docker compose exec -T postgres psql -U eve -d postgres \
       -tAc "SELECT 1 FROM pg_database WHERE datname='eve_market_test'" 2>/dev/null | grep -q 1; then
    green "eve_market_test already exists"
  elif docker compose exec -T postgres createdb -U eve eve_market_test 2>/dev/null; then
    green "created eve_market_test"
  else
    yellow "could not create eve_market_test — database tests will skip"
  fi
else
  yellow "Docker unavailable; create a database named eve_market_test yourself,"
  yellow "or set EVE_TEST_DATABASE_URL. Database tests skip without it."
fi

# --- 7. Static Data Export -------------------------------------------------
# The SDE gives us item names and packaged volumes. Without it everything
# still works, but items show as numeric type ids and hauling can't compute
# ISK/m3. Fuzzwork stopped publishing per-table CSVs, so `fetch-sde` streams
# its whole-database SQLite dump and reads the one table we need.
if [[ "$SKIP_SDE" == "0" ]]; then
  step "Loading static data (item names and volumes)"
  if eve-market fetch-sde; then
    green "static data loaded"
  else
    yellow "fetch-sde failed — re-run 'eve-market fetch-sde' later (is Postgres up?)"
  fi
fi

# --- 8. Verify -------------------------------------------------------------
step "Running doctor"
eve-market doctor || true

step "Done"
cat <<'EOF'

Next:
  source .venv/bin/activate
  eve-market snapshot the_forge     # pull Jita's order book (takes a minute)
  eve-market spreads the_forge      # rank station-trading spreads

If doctor flagged anything, see "Setup on your PC" in README.md.
EOF

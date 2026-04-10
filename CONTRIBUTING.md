# Contributing to PFV2

## Prerequisites

- Docker & Docker Compose
- Git

## Quick Start

```bash
# 1. Clone and configure
git clone <repo-url> && cd pfv
cp .env.example .env

# 2. Start the dev stack (MySQL + Redis + Backend + Frontend + Nginx)
./pfv start

# 3. Open the app
open http://localhost
```

The first user to register becomes the org owner and superadmin.

## Seeding Mock Data

The seed script creates a realistic dataset: 5 accounts, 100+ transactions across 3 months, recurring templates, billing periods, and budgets.

**Default seed (creates a "demo" user):**

```bash
./pfv seed
# Login: demo / demo1234
```

**Custom seed (your own user):**

```bash
SEED_USERNAME=flamarion \
SEED_PASSWORD=abcd1234 \
SEED_EMAIL=flamarion@example.com \
SEED_FIRST_NAME=Flamarion \
SEED_LAST_NAME=Jorge \
SEED_ORG="FJ Consulting" \
./pfv seed
```

**Important:** The seed script will register the user if it doesn't exist, then log in and create data. If the user already exists, it logs in with the provided credentials and adds data to their org.

### Seed Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SEED_USERNAME` | `demo` | Username for the seeded user |
| `SEED_PASSWORD` | `demo1234` | Password |
| `SEED_EMAIL` | `demo@example.com` | Email address |
| `SEED_FIRST_NAME` | `Demo` | First name |
| `SEED_LAST_NAME` | `User` | Last name |
| `SEED_ORG` | `Demo Household` | Organization name |

## CLI Reference

| Command | Description |
|---------|-------------|
| `./pfv start` | Build and start all services (development) |
| `./pfv stop` | Stop all services |
| `./pfv restart` | Restart without rebuild |
| `./pfv rebuild` | Force rebuild (no cache) and start |
| `./pfv reset` | Destroy all data, rotate JWT secret, start fresh |
| `./pfv prod` | Build and start in production mode |
| `./pfv migrate` | Run pending database migrations |
| `./pfv logs [svc]` | View logs (backend, frontend, nginx, mysql, redis) |
| `./pfv status` | Show container status |
| `./pfv shell [svc]` | Open a shell in a service (default: backend) |
| `./pfv seed` | Populate with mock data |

## Architecture

```
Browser → nginx (:80) → /api/*  → backend (FastAPI :8000) → MySQL (:3306)
                      → /*      → frontend (Next.js :3000)
                                  backend → Redis (:6379)
```

## Creating Migrations

```bash
docker compose exec backend alembic revision -m "description"
```

Then edit the generated file in `backend/alembic/versions/`.

## Development vs Production

| Aspect | Dev (`./pfv start`) | Prod (`./pfv prod`) |
|--------|-------|------|
| Frontend | Next.js dev server (hot reload) | Standalone build (`node server.js`) |
| Backend | uvicorn with `--reload` | uvicorn with 2 workers |
| Migrations | Auto-run on backend startup | Separate init service (runs first) |
| Volumes | Source code mounted | Immutable containers |
| Redis | Included | Included |

## Branching

- Never push directly to `main` — always branch + PR
- Feature branches: `feat/<name>`
- Fix branches: `fix/<name>`

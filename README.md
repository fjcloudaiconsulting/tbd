# PFV2

Personal finance management application.

## Stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2.0 (async), Alembic
- **Frontend:** Next.js 15, React 19, TypeScript, Tailwind CSS
- **Database:** MySQL 8.0
- **Reverse proxy:** nginx

## Quick Start

```bash
cp .env.example .env    # first time only
./pfv start             # build, start, run migrations
```

App: http://localhost

## Commands

```bash
./pfv start             # build and start all services
./pfv stop              # stop all services
./pfv restart           # restart without rebuild
./pfv rebuild           # force rebuild (no cache)
./pfv reset             # destroy all data and start fresh
./pfv migrate           # run pending migrations
./pfv logs [service]    # view logs (backend, frontend, nginx, mysql)
./pfv status            # container status
./pfv shell [service]   # shell into a container (default: backend)
```

## Architecture

```
Browser -> nginx (:80) -> /api/*  -> backend (FastAPI :8000) -> MySQL (:3306)
                       -> /*      -> frontend (Next.js :3000)
```

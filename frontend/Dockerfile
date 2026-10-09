# --- Build stage ---
FROM node:22-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS builder

WORKDIR /app

# pnpm version comes from packageManager in package.json.
RUN corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .

# No build args: config is runtime-only (TBD_* env, see lib/runtime-config.ts), so one
# image serves every environment. Canonical list: docs/operations/ENVIRONMENT.md.
ENV NEXT_TELEMETRY_DISABLED=1
RUN pnpm build

# --- Runtime stage ---
FROM node:22-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS runner

WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

RUN addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 nextjs

# Copy standalone server + static assets from build
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public

# The only baked value (RELEASE_CONTRACT.md section 3): build-image passes it,
# lib/runtime-config.ts reads it. After the COPYs so they stay cached. A plain build gets "dev".
ARG APP_VERSION=dev
ENV TBD_APP_VERSION=$APP_VERSION

USER nextjs

EXPOSE 3000

CMD ["node", "server.js"]

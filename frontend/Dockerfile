# --- Build stage ---
FROM node:22-slim AS builder

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install

COPY . .

# NEXT_PUBLIC_* envs must be declared as ARGs so DO App Platform's
# RUN_AND_BUILD_TIME scope can inject them at build time. Next.js inlines
# process.env.NEXT_PUBLIC_* into the static JS bundle at build, not runtime.
# Adding a new NEXT_PUBLIC_* env requires (a) adding it to .do/app.yaml,
# (b) adding an ARG + ENV line here, (c) doctl apps update to rebuild.
# Canonical list lives in ENVIRONMENT.md (Frontend section).
ARG NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

ARG NEXT_PUBLIC_SITE_URL
ENV NEXT_PUBLIC_SITE_URL=$NEXT_PUBLIC_SITE_URL

ARG NEXT_PUBLIC_GOOGLE_SSO_ENABLED
ENV NEXT_PUBLIC_GOOGLE_SSO_ENABLED=$NEXT_PUBLIC_GOOGLE_SSO_ENABLED

ARG NEXT_PUBLIC_APP_VERSION
ENV NEXT_PUBLIC_APP_VERSION=$NEXT_PUBLIC_APP_VERSION

ARG NEXT_PUBLIC_CAPTCHA_PROVIDER
ENV NEXT_PUBLIC_CAPTCHA_PROVIDER=$NEXT_PUBLIC_CAPTCHA_PROVIDER

ARG NEXT_PUBLIC_CAPTCHA_SITE_KEY
ENV NEXT_PUBLIC_CAPTCHA_SITE_KEY=$NEXT_PUBLIC_CAPTCHA_SITE_KEY

ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# --- Runtime stage ---
FROM node:22-slim AS runner

WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

RUN addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 nextjs

# Copy standalone server + static assets from build
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public

USER nextjs

EXPOSE 3000

CMD ["node", "server.js"]

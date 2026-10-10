# Web image. Build from the repository root (docker-compose.yml does):
#   docker build -f apps/web/Dockerfile .
# RISK_API_URL is read at request time by the server-side proxy routes only.
FROM node:22-bookworm-slim

ENV NEXT_TELEMETRY_DISABLED=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

WORKDIR /workspace
# Activate the exact pnpm from package.json "packageManager" at build time.
RUN corepack enable && corepack prepare pnpm@11.19.0 --activate

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/
COPY packages/contracts/package.json packages/contracts/
RUN pnpm install --frozen-lockfile

COPY packages/contracts packages/contracts
COPY apps/web apps/web
RUN pnpm --filter @risk0/web build

ENV NODE_ENV=production
EXPOSE 3000
CMD ["pnpm", "--filter", "@risk0/web", "exec", "next", "start", "--hostname", "0.0.0.0", "--port", "3000"]

# syntax=docker/dockerfile:1
# RepoLens web app (Next.js standalone server).
#   docker build -f infra/docker/web/Dockerfile -t repolens-web .
# REPOLENS_API_TOKEN is passed at run time, never baked into the image.

ARG NODE_IMAGE=node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402

# Install the workspace from the lockfile.
FROM ${NODE_IMAGE} AS deps
WORKDIR /repo
COPY package.json package-lock.json ./
COPY apps/web/package.json ./apps/web/package.json
RUN npm ci --workspace apps/web --include-workspace-root --ignore-scripts --no-audit --no-fund

FROM ${NODE_IMAGE} AS build
WORKDIR /repo
ENV NEXT_TELEMETRY_DISABLED=1 \
    REPOLENS_WEB_STANDALONE=1
COPY --from=deps /repo/node_modules ./node_modules
COPY package.json package-lock.json ./
COPY apps/web ./apps/web
RUN npm --workspace apps/web run build

# Runtime: only the traced standalone output.
FROM ${NODE_IMAGE} AS runtime
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3100 \
    REPOLENS_API_URL=http://api:8100
WORKDIR /app
COPY --from=build --chown=root:root /repo/apps/web/.next/standalone ./
COPY --from=build --chown=root:root /repo/apps/web/.next/static ./apps/web/.next/static
# Unprivileged user shipped with the node image (uid 1000).
USER node
EXPOSE 3100
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:3100/').then(r=>process.exit(r.ok?0:1),()=>process.exit(1))"]
CMD ["node", "apps/web/server.js"]

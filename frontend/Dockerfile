ARG PNPM_VERSION=11.19.0

FROM node:24.19.0-alpine3.23@sha256:244cc2b53f46f9e876304391d17682b0ddae9ac33491f4857e25e35a36ba7995 AS dependencies
ARG PNPM_VERSION
WORKDIR /app
RUN corepack enable \
    && corepack prepare pnpm@${PNPM_VERSION} --activate \
    && test "$(pnpm --version)" = "${PNPM_VERSION}"
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

FROM node:24.19.0-alpine3.23@sha256:244cc2b53f46f9e876304391d17682b0ddae9ac33491f4857e25e35a36ba7995 AS builder
ARG PNPM_VERSION
WORKDIR /app
RUN corepack enable
COPY --from=dependencies /root/.cache/node/corepack /root/.cache/node/corepack
COPY --from=dependencies /app/node_modules ./node_modules
COPY . .
RUN corepack prepare pnpm@${PNPM_VERSION} --activate \
    && test "$(pnpm --version)" = "${PNPM_VERSION}" \
    && pnpm test \
    && pnpm typecheck \
    && pnpm build \
    && node scripts/smoke-next-standalone.mjs .next/standalone

FROM node:24.19.0-alpine3.23@sha256:244cc2b53f46f9e876304391d17682b0ddae9ac33491f4857e25e35a36ba7995 AS runner
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
WORKDIR /app
RUN addgroup --system --gid 1001 nodejs \
    && adduser --system --uid 1001 nextjs
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public ./public
USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]

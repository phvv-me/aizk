# The TS server experiment: SvelteKit built with adapter-node, served by plain node. The
# builder installs with pnpm from the chefe-generated package.json and lockfile.

FROM node:26-slim AS builder
RUN corepack enable pnpm
WORKDIR /app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY svelte.config.js vite.config.ts tsconfig.json ./
COPY src ./src
RUN pnpm exec vite build

FROM node:26-slim
WORKDIR /app
COPY --from=builder /app/build ./build
COPY --from=builder /app/package.json .
COPY --from=builder /app/node_modules ./node_modules
EXPOSE 3000
CMD ["node", "build"]

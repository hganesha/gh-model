FROM node:22-slim
WORKDIR /srv
COPY . .
RUN corepack enable && pnpm install --frozen-lockfile && pnpm --filter @acme/api build
CMD ["node", "apps/api/dist/server.js"]

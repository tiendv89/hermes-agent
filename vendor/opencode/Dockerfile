FROM node:22-alpine

RUN corepack enable

WORKDIR /app

COPY package.json pnpm-lock.yaml ./
RUN corepack install && pnpm install --prod --frozen-lockfile

EXPOSE 4096

CMD ["pnpm", "exec", "opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096", "--print-logs"]

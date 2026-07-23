# Enki Governance — Production UI
# Multi-stage build: install deps → build SvelteKit → slim runtime

# ── Stage 1: Install dependencies ──────────────────────────────────────────
FROM node:20-alpine AS deps
WORKDIR /app
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --production=false

# ── Stage 2: Build SvelteKit ───────────────────────────────────────────────
FROM node:20-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY ui/ .
# Build environment variables (compile-time)
ARG VITE_API_PROXY=http://api:8000
ENV VITE_API_PROXY=$VITE_API_PROXY
RUN npm run build
# Prune dev dependencies
RUN npm prune --production

# ── Stage 3: Production runtime ────────────────────────────────────────────
FROM node:20-alpine AS runtime
WORKDIR /app

# Non-root user
RUN addgroup -S enki && adduser -S enki -G enki

# Copy only what's needed to run
COPY --from=build /app/build ./build
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/package.json ./

# Runtime environment
ENV NODE_ENV=production
ENV PORT=3000
ENV ORIGIN=http://localhost:3000
ENV PROTOCOL_HEADER=X-Forwarded-Proto
ENV HOST_HEADER=X-Forwarded-Host

# Switch to non-root
USER enki

EXPOSE 3000

# SvelteKit adapter-node serves on PORT
CMD ["node", "build"]

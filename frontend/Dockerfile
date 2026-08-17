# syntax=docker/dockerfile:1
# Build stage: a Vite SPA has no server-side runtime, so the only thing the
# final image needs is the static build output plus something to serve it —
# node isn't needed past this stage.
FROM node:20-slim AS builder
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
# The API base URL is compiled into the JS bundle at build time
# (import.meta.env), not read at container runtime — it has to be a URL the
# user's *browser* can reach, not an internal Docker service name, so it's
# passed as a build arg rather than a runtime env var. See docker-compose.yml.
ARG VITE_API_BASE_URL=http://localhost:8000/api/v1
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD wget -q -O /dev/null http://localhost/ || exit 1

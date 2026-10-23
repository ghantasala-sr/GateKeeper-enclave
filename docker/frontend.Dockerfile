# Gatekeeper frontend (Next.js standalone server).
#
# Build from the gatekeeper-enclave repo root, with the app source as the context:
#   docker build -f docker/frontend.Dockerfile -t gatekeeper-frontend:local \
#     --build-arg NEXT_PUBLIC_API_URL=http://localhost:8000 gatekeeper
#
# Next.js bakes NEXT_PUBLIC_* values into the JavaScript at build time, so the API URL is
# a build argument. Fonts (next/font/google) are downloaded during the build and served
# from the image, so the running container never contacts Google Fonts.

FROM node:22-bookworm-slim AS build
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
RUN npm run build

FROM node:22-bookworm-slim
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
WORKDIR /app
# The standalone output contains the server and only the node_modules it needs.
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
# Numeric (node is uid 1000): Kubernetes' runAsNonRoot can only verify a numeric user.
USER 1000
EXPOSE 3000
CMD ["node", "server.js"]

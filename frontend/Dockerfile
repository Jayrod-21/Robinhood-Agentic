# Frontend image — Next.js 14 dev server.
#
# Runs `next dev` (not a production build) so NEXT_PUBLIC_API_URL — which carries the randomly
# chosen backend port — can be injected at container start rather than baked into the image. Same
# approach as the sibling 3a dashboard.
FROM node:20-slim

WORKDIR /app

COPY package.json package-lock.json* ./
# npm ci (not install) for a reproducible tree: it installs exactly the committed lockfile and fails
# if the lock and manifest disagree, instead of silently mutating the lock.
RUN npm ci

COPY . .

ENV NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_TELEMETRY_DISABLED=1

# Run as non-root.
RUN groupadd -r nodejs && useradd -r -g nodejs -m nodejs && chown -R nodejs:nodejs /app
USER nodejs

EXPOSE 3000

CMD ["npx", "next", "dev", "-H", "0.0.0.0", "-p", "3000"]

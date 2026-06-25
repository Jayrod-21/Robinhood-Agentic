/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // NEXT_PUBLIC_API_URL is read at server start (dev mode), so the randomly-chosen backend port
  // can be injected at `docker compose up` time rather than baked into the image.
};
module.exports = nextConfig;

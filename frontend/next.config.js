/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // NEXT_PUBLIC_API_URL is read at server start (dev mode), so the randomly-chosen backend port
  // can be injected at `docker compose up` time rather than baked into the image.

  // Thirteen top-level tabs collapsed into five themed sections. Every old path still resolves,
  // server-side, because those URLs are in bookmarks, in the cycle reports under logs/reports/,
  // and in issue threads. A consolidation that quietly 404s a year of links is a regression
  // wearing a redesign.
  //
  // NOT permanent (308): these are one release old and the shape may still move. A 308 is cached
  // by the browser indefinitely and would outlive any correction.
  //
  // /debate and /position are deliberately absent: /debate/[id] and /position/[symbol] are the
  // drill-downs this whole change is modelled on and they keep their URLs. Only the bare /debate
  // index moved, and a redirect on it would swallow its children.
  async redirects() {
    return [
      { source: "/market", destination: "/research", permanent: false },
      { source: "/scan", destination: "/research/scan", permanent: false },
      { source: "/fundamentals", destination: "/research/fundamentals", permanent: false },
      { source: "/pipeline", destination: "/decisions/pipeline", permanent: false },
      { source: "/performance", destination: "/track-record", permanent: false },
      { source: "/learning", destination: "/track-record/accuracy", permanent: false },
      { source: "/calibration", destination: "/track-record/calibration", permanent: false },
      { source: "/testing-lab", destination: "/track-record/lab", permanent: false },
      { source: "/reconciliation", destination: "/", permanent: false },
      // Exact path only — Next matches literally, so this does NOT catch /debate/<id>.
      { source: "/debate", destination: "/decisions", permanent: false },
    ];
  },
};
module.exports = nextConfig;

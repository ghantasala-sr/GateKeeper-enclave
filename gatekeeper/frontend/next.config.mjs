/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: "standalone", // self-contained server for the container image
  devIndicators: false, // no dev badge in screenshots and demo recordings
  // The access token travels in the URL fragment after login; never leak the
  // page URL to other sites.
  async headers() {
    return [{ source: "/:path*", headers: [{ key: "Referrer-Policy", value: "no-referrer" }] }];
  },
};

export default nextConfig;

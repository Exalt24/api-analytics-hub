/** @type {import('next').NextConfig} */
// The API base is read at build time and exposed to the browser, because the
// dashboard talks to the backend directly rather than proxying through Next. That
// keeps one place where CORS and the bearer token are configured.
const nextConfig = { reactStrictMode: true };
export default nextConfig;

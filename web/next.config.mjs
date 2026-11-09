/** @type {import('next').NextConfig} */
// The API base is read at build time and exposed to the browser, because the
// dashboard talks to the backend directly rather than proxying through Next. That
// keeps one place where CORS and the bearer token are configured.
// The report export runs in Server Actions, which load these packages in Node, so they
// stay out of the bundle.
const nextConfig = { reactStrictMode: true, serverExternalPackages: ["pg", "exceljs", "pdf-lib"] };
export default nextConfig;

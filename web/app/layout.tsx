import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Analytics Hub",
  description: "Multi-tenant analytics over stored platform data",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

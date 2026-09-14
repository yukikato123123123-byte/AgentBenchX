import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = { title: "AgentBenchX · Evaluation control plane", description: "Reproducible AI agent evaluation" };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}

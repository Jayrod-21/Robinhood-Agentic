import type { Metadata } from "next";
import "./globals.css";
import { Shell } from "@/components/shell";
import { AccountProvider } from "@/components/account-context";

export const metadata: Metadata = {
  title: "Agentic Alpaca Dashboard",
  description: "Live read-only monitor + Sprinkle Sauce screen + jury debate engine for the Agentic account.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <AccountProvider>
          <Shell>{children}</Shell>
        </AccountProvider>
      </body>
    </html>
  );
}

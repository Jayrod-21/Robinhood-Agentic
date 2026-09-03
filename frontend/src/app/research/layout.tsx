import { SubTabs } from "@/components/sub-tabs";

// Research — what's out there and what's worth a look. One funnel in three stages: the market
// backdrop, the screen over the universe, then the per-name financials. Fundamentals was a
// top-level tab despite being the drill-down of a Scan survivor.
const TABS = [
  { href: "/research", label: "Market" },
  { href: "/research/scan", label: "Scan" },
  { href: "/research/fundamentals", label: "Fundamentals" },
];

export default function ResearchLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <SubTabs tabs={TABS} />
      {children}
    </>
  );
}

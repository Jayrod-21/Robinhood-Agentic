import { SubTabs } from "@/components/sub-tabs";

// Decisions — how a call gets made, live and past. The pipeline's own description is "one ticker
// through the full chain: real screen → bull/bear → live jury → sized decision", which is to say
// it already contained the debate; they were two tabs over one flow.
const TABS = [
  { href: "/decisions", label: "Debate" },
  { href: "/decisions/pipeline", label: "Pipeline" },
];

export default function DecisionsLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <SubTabs tabs={TABS} />
      {children}
    </>
  );
}

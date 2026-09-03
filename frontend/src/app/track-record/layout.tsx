import { SubTabs } from "@/components/sub-tabs";

// Track record — was it right, and did it make money. Four cuts of one underlying thing (past
// judgments and what they were worth): by money, by accuracy, by confidence honesty, by model.
// docs/contracts/learning-endpoint.md already conceded the Accuracy/Calibration split, describing
// Learning as carrying "a compact calibration read (the full reliability curve stays on
// /calibration)" — one page reported across two tabs.
const TABS = [
  { href: "/track-record", label: "Returns" },
  { href: "/track-record/accuracy", label: "Accuracy" },
  { href: "/track-record/calibration", label: "Calibration" },
  { href: "/track-record/lab", label: "Testing Lab" },
];

export default function TrackRecordLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <SubTabs tabs={TABS} />
      {children}
    </>
  );
}

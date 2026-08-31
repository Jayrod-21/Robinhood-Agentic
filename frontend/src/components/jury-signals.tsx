// Shared read-outs for a jury panel's meta-signals: how the model families compared (#142) and
// the calibration warnings that say a verdict should be read with suspicion (#139). These ANNOTATE
// a result; none of them changes the decision, which stays a family-agnostic vote count. Used by
// both the live debate view and the archived-debate detail page so the two never drift.

import { AlertTriangle } from "lucide-react";
import { Badge, Card, CardBody, CardHeader, CardTitle, decisionTone } from "@/components/ui";
import type { FamilySummary, Vote } from "@/lib/types";

const VOTE_ORDER: Vote[] = ["BUY", "SELL", "HOLD"];

// Below this agreement the families are not reading one question the same way. Mirrors the backend's
// _FAMILY_AGREEMENT_FLOOR (app/debate/calibration.py); kept in sync by hand, low stakes if it drifts
// (it only tints a line amber).
const FAMILY_AGREEMENT_FLOOR = 0.6;

/** "anthropic" -> "Claude", "google" -> "Gemini"; anything else is title-cased as-is so a new
 *  provider still renders a sensible label instead of a raw slug. */
export function providerLabel(provider?: string): string {
  if (!provider) return "";
  const known: Record<string, string> = { anthropic: "Claude", google: "Gemini", openai: "GPT" };
  return known[provider] ?? provider.charAt(0).toUpperCase() + provider.slice(1);
}

function tallyTotal(counts: Partial<Record<Vote, number>>): number {
  return VOTE_ORDER.reduce((sum, v) => sum + (counts[v] ?? 0), 0);
}

/** The paired-panel family split: per-family vote counts and how often the families agreed, lens by
 *  lens. Renders nothing for a single-family jury (there is nothing to compare). The whole point of
 *  running ten lenses twice is that a disagreement then has one explanation: same lens, same
 *  evidence, different model. */
export function FamilySplit({ families }: { families?: FamilySummary }) {
  const providers = families?.providers ?? {};
  const names = Object.keys(providers);
  if (names.length < 2) return null;

  const { paired_lenses, lenses_agreed, agreement, disagreed_on } = families!;
  const lowAgreement = agreement != null && agreement < FAMILY_AGREEMENT_FLOOR;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Model families</CardTitle>
        <span className="text-xs text-zinc-500">
          same lenses, judged by each family
        </span>
      </CardHeader>
      <CardBody className="space-y-3">
        <div className="space-y-2">
          {names.sort().map((name) => {
            const counts = providers[name];
            return (
              <div key={name} className="flex items-center justify-between gap-3">
                <span className="text-sm font-medium text-zinc-300">
                  {providerLabel(name)}
                  <span className="ml-2 text-xs font-normal text-zinc-600">
                    {tallyTotal(counts)} lens{tallyTotal(counts) === 1 ? "" : "es"}
                  </span>
                </span>
                <span className="flex items-center gap-1.5">
                  {VOTE_ORDER.map((v) => (
                    <span key={v} className="flex items-center gap-1 text-xs tnum text-zinc-500">
                      <Badge tone={decisionTone(v)}>{v}</Badge>
                      {counts[v] ?? 0}
                    </span>
                  ))}
                </span>
              </div>
            );
          })}
        </div>

        {paired_lenses > 0 && (
          <div className={cnAgreement(lowAgreement)}>
            {agreement != null ? (
              <>
                Families agreed on {lenses_agreed} of {paired_lenses} paired lens
                {paired_lenses === 1 ? "" : "es"} ({(agreement * 100).toFixed(0)}%)
                {lowAgreement && (
                  <span className="ml-1 inline-flex items-center gap-1">
                    <AlertTriangle className="h-3 w-3" />
                    the families are not reading this one the same way
                  </span>
                )}
              </>
            ) : (
              <>No lens was judged by both families, so there is nothing to compare.</>
            )}
          </div>
        )}

        {disagreed_on.length > 0 && (
          <div className="text-xs text-zinc-500">
            <span className="text-zinc-600">Split on:</span>{" "}
            {disagreed_on.map((l) => l.replace(/_/g, " ")).join(", ")}
          </div>
        )}

        <p className="text-xs leading-relaxed text-zinc-600">
          A cross-family agreement is not extra confidence until there are enough debates to know how
          often they diverge; a split is a finding, not a tie to break.
        </p>
      </CardBody>
    </Card>
  );
}

function cnAgreement(low: boolean): string {
  return low ? "text-sm text-flat" : "text-sm text-zinc-400";
}

/** "Read this jury with suspicion": the calibration signals verbatim (they are written to be shown
 *  to an operator as-is). Renders nothing when the panel is healthy. */
export function CalibrationBanner({ signals }: { signals?: string[] }) {
  if (!signals || signals.length === 0) return null;
  return (
    <Card className="border-flat/40 bg-flat/5">
      <CardBody className="pt-5">
        <div className="flex items-center gap-2 font-medium text-flat">
          <AlertTriangle className="h-4 w-4" /> Read this jury with suspicion
        </div>
        <ul className="mt-2 space-y-1 text-sm text-zinc-400">
          {signals.map((sig, i) => (
            <li key={i}>· {sig}</li>
          ))}
        </ul>
      </CardBody>
    </Card>
  );
}

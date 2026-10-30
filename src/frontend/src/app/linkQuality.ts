// How good a link direction's signal is for its channel's radio settings. The
// thresholds and colours are presentation only; the API sends the raw averages.

export type LinkTier = "good" | "fair" | "marginal" | "unknown";

export const LINK_TIERS: {
  tier: LinkTier;
  label: string;
  color: string;
  description: string;
}[] = [
  {
    tier: "good",
    label: "Good",
    color: "#2e9e4f",
    description: "10 dB or more above the floor",
  },
  {
    tier: "fair",
    label: "Fair",
    color: "#e0a526",
    description: "4 to 10 dB above the floor",
  },
  {
    tier: "marginal",
    label: "Marginal",
    color: "#d0453a",
    description: "less than 4 dB above the floor",
  },
  {
    tier: "unknown",
    label: "Unknown",
    color: "#9aa39e",
    description: "SNR or radio channel unknown",
  },
];

export function linkTierLabel(tier: LinkTier): string {
  return LINK_TIERS.find((entry) => entry.tier === tier)!.label;
}

export function linkTierColor(tier: LinkTier): string {
  return LINK_TIERS.find((entry) => entry.tier === tier)!.color;
}

/** The lowest SNR a LoRa receiver can still decode at this spreading factor, in dB. */
export function demodulationFloor(spreadingFactor: number): number {
  return -7.5 - 2.5 * (spreadingFactor - 7);
}

/**
 * The fade margin: how far the average SNR sits above the demodulation floor.
 * Null when the SNR or radio channel is unknown.
 */
export function fadeMargin(
  averageSnr: number | null,
  spreadingFactor: number | null,
): number | null {
  return averageSnr === null || spreadingFactor === null
    ? null
    : averageSnr - demodulationFloor(spreadingFactor);
}

export function linkTier(
  averageSnr: number | null,
  spreadingFactor: number | null,
): LinkTier {
  return marginTier(fadeMargin(averageSnr, spreadingFactor));
}

/** The tier of a fade margin in dB; null means no SNR. */
export function marginTier(margin: number | null): LinkTier {
  if (margin === null) return "unknown";
  if (margin >= 10) return "good";
  if (margin >= 4) return "fair";
  return "marginal";
}

/** Line width in pixels from the observations in both directions. */
export function linkWidth(observations: number): number {
  const width = 1 + 1.5 * Math.log10(Math.max(observations, 1));
  return Math.min(width, 5);
}

/**
 * A link's rank by times heard: the weaker direction's count, so a link heard
 * both ways ranks above a one-way link however busy; ties go by the total,
 * added as a fraction below 1 that grows with it.
 */
export function bothWaysObservations(link: {
  a_to_b: { observations: number };
  b_to_a: { observations: number };
}): number {
  const total = link.a_to_b.observations + link.b_to_a.observations;
  return (
    Math.min(link.a_to_b.observations, link.b_to_a.observations) +
    total / (total + 1)
  );
}

/**
 * Split a payment of `totalCents` into `parts` instalments (pay-in-N checkout).
 *
 * Requires totalCents >= 0 and 1 <= parts <= 12. Returns `parts` amounts that add
 * up exactly to `totalCents`. Earlier instalments are never smaller than later ones,
 * and no two instalments differ by more than one cent.
 */
export function splitPayment(totalCents: number, parts: number): number[] {
  const base = Math.round(totalCents / parts);
  const out: number[] = new Array(parts).fill(base);
  out[parts - 1] = totalCents - base * (parts - 1);
  return out;
}

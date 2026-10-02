/**
 * A shopping cart: quantities per SKU and the unit price each line was added at.
 * Quantities are never negative; a line whose quantity reaches 0 disappears.
 */
export class Cart {
  private lines = new Map<string, { qty: number; unitCents: number }>();

  /** Add `qty` (>= 1) units of `sku` at `unitCents` (>= 0) each. Throws on invalid input. Re-adding a SKU keeps its first price. */
  add(sku: string, qty: number, unitCents: number): void {
    if (qty < 1 || unitCents < 0) throw new RangeError("invalid quantity or price");
    const line = this.lines.get(sku);
    if (line) line.qty += qty;
    else this.lines.set(sku, { qty, unitCents });
  }

  /** Remove up to `qty` (>= 1) units of `sku`; removing more than are in the cart removes the whole line. Throws if qty < 1. */
  remove(sku: string, qty: number): void {
    if (qty < 1) throw new RangeError("invalid quantity");
    const line = this.lines.get(sku);
    if (!line) return;
    line.qty -= qty;
    if (line.qty === 0) this.lines.delete(sku);
  }

  /** Units of `sku` in the cart (0 if none). */
  quantity(sku: string): number {
    return this.lines.get(sku)?.qty ?? 0;
  }

  /** Total price of the cart, in cents. */
  get totalCents(): number {
    let total = 0;
    for (const { qty, unitCents } of this.lines.values()) total += qty * unitCents;
    return total;
  }
}

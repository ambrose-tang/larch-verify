/**
 * Compare two dotted version strings numerically, component by component.
 * Missing components count as 0, so "1.2" equals "1.2.0".
 * Returns -1 if a < b, 0 if equal, 1 if a > b.
 * Requires both strings to be non-empty dot-separated runs of decimal digits.
 *
 * @param {string} a
 * @param {string} b
 * @returns {number}
 */
function compareVersions(a, b) {
  const pa = a.split(".").map(Number);
  const pb = b.split(".").map(Number);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] || 0;
    const y = pb[i] || 0;
    if (x !== y) return x < y ? -1 : 1;
  }
  return 0;
}

module.exports = { compareVersions };

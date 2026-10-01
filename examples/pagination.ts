/**
 * Number of pages needed to show `items` items with at most `perPage` items per page.
 * Zero items need zero pages; a partially filled last page still counts as a page.
 * Requires items >= 0 and perPage >= 1.
 *
 * pageCount(0, 10) === 0, pageCount(10, 10) === 1, pageCount(11, 10) === 2
 */
export function pageCount(items: number, perPage: number): number {
  return Math.floor((items + perPage) / perPage);
}

/**
 * Index range [start, end) of the items shown on 1-based page `page`.
 * Pages past the end give an empty range at `items`.
 * Requires items >= 0, perPage >= 1 and page >= 1.
 */
export function pageRange(items: number, perPage: number, page: number): [number, number] {
  const start = Math.min((page - 1) * perPage, items);
  const end = Math.min(start + perPage, items);
  return [start, end];
}

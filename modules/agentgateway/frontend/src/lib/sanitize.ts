/**
 * Defense-in-depth: even if the backend stripper missed a `<memory_update>`
 * block, this function removes any visible trace before render. It must
 * tolerate fragmentary input (the open or close tag may have been split
 * across token boundaries upstream) — when only a partial open tag is
 * present, drop the trailing fragment so the user does not see "<mem".
 */
const OPEN = "<memory_update>";
const CLOSE = "</memory_update>";

export function stripMemoryUpdate(text: string): string {
  if (!text) return text;
  let out = text;
  // Strip every fully-formed block first.
  while (true) {
    const start = out.indexOf(OPEN);
    if (start === -1) break;
    const end = out.indexOf(CLOSE, start + OPEN.length);
    if (end === -1) {
      // Unterminated open — chop everything from the open onwards.
      out = out.slice(0, start);
      return out.replace(/\s+$/, "");
    }
    out = out.slice(0, start) + out.slice(end + CLOSE.length);
  }
  // Drop a trailing fragment that could become an open tag.
  for (let i = OPEN.length - 1; i > 0; i--) {
    const tail = out.slice(-i);
    if (OPEN.startsWith(tail) && tail.startsWith("<")) {
      out = out.slice(0, -i);
      break;
    }
  }
  return out;
}

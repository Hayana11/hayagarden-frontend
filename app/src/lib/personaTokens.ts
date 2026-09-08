/**
 * Local persona token estimate.
 * Matches backend `estimate_tokens_heuristic_cjk1_ascii4_v1`:
 * CJK ≈ 1 token, other text ≈ 4 characters / token, rounded up.
 */
export function estimatePersonaTokens(text: string): number {
  if (!text) return 0;
  let cjk = 0;
  let other = 0;
  for (const ch of text) {
    const code = ch.codePointAt(0) || 0;
    if (
      (code >= 0x4E00 && code <= 0x9FFF)
      || (code >= 0x3400 && code <= 0x4DBF)
      || (code >= 0xF900 && code <= 0xFAFF)
      || (code >= 0x2E80 && code <= 0x2EFF)
      || (code >= 0x3000 && code <= 0x303F)
      || (code >= 0xFF00 && code <= 0xFFEF)
      || (code >= 0x3040 && code <= 0x30FF)
      || (code >= 0xAC00 && code <= 0xD7AF)
    ) {
      cjk += 1;
    } else {
      other += 1;
    }
  }
  return other ? cjk + Math.ceil(other / 4) : cjk;
}

const INTERNAL_HOSTS = new Set(['workbench.aceswarm.invalid', 'apps.aceswarm.invalid']);
const BLOCKED_SCHEMES = /^(?:javascript|data|blob|file|about|mailto|tel|intent|content):/i;
const EXPLICIT_SCHEME = /^[a-zA-Z][a-zA-Z\d+.-]*:(?!\d+(?:\/|$|\?|#))/;

export function normalizePageUrl(raw) {
  const trimmed = String(raw || '').trim();
  if (!trimmed || BLOCKED_SCHEMES.test(trimmed)) return '';
  const candidate = /^https?:\/\//i.test(trimmed)
    ? trimmed
    : (EXPLICIT_SCHEME.test(trimmed) ? trimmed : 'http://' + trimmed);
  try {
    const parsed = new URL(candidate);
    const host = parsed.hostname.toLowerCase();
    if ((parsed.protocol !== 'http:' && parsed.protocol !== 'https:') || !host) return '';
    if (INTERNAL_HOSTS.has(host) || host.includes('_')) return '';
    if (parsed.username || parsed.password) return '';
    if (parsed.port === '0' || /^0+$/.test(parsed.port)) return '';
    return parsed.toString();
  } catch (_) {
    return '';
  }
}

export function sameOrigin(a, b) {
  const left = normalizePageUrl(a);
  const right = normalizePageUrl(b);
  if (!left || !right) return false;
  return new URL(left).origin === new URL(right).origin;
}

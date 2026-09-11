import type { Row } from './core';

export type VerifiedBuild = {
  id: string;
  label: string;
  fingerprint: string;
};

export type SiteConnectionCheck = {
  ok: boolean;
  checked_at: string;
  version: string | null;
  endpoints: string[];
  error?: {
    category: string;
    reason?: string;
    message: string;
    endpoint?: string;
    method?: string;
    http_status?: number;
  } | null;
};

export type Site = Row & {
  display_id?: number;
  verified_version?: string | null;
  verified_build?: VerifiedBuild | null;
  adapter_notice?: string;
  distribution_ready?: boolean;
  distribution_issues?: string[];
  enabled_template_count?: number;
  connection_check?: SiteConnectionCheck | null;
};

export function siteConnectionErrorMessage(
  error: SiteConnectionCheck['error'],
  t: (zh: string, en: string) => string,
) {
  let reasonMessage = '';
  switch (error?.reason) {
    case 'tls_certificate_expired':
      reasonMessage = t(
        '目标站点的 HTTPS 证书已过期，请联系站点管理员续期证书',
        'The site’s HTTPS certificate has expired. Contact the site administrator to renew it.',
      );
      break;
    case 'tls_certificate_not_yet_valid':
      reasonMessage = t(
        '目标站点的 HTTPS 证书尚未生效，请检查系统时间或联系站点管理员',
        'The site’s HTTPS certificate is not yet valid. Check the system time or contact the site administrator.',
      );
      break;
    case 'tls_certificate_invalid':
      reasonMessage = t(
        '目标站点的 HTTPS 证书验证失败，请联系站点管理员检查证书配置',
        'The site’s HTTPS certificate could not be verified. Contact the site administrator to check the certificate configuration.',
      );
      break;
  }
  const message =
    reasonMessage ||
    (typeof error?.message === 'string' && error.message.trim()) ||
    t('尚未完成接口连通验证，请重试。', 'Connection validation is incomplete. Retry.');
  const endpoint = typeof error?.endpoint === 'string' ? error.endpoint.trim() : '';
  const method =
    typeof error?.method === 'string' && /^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)$/.test(error.method)
      ? error.method
      : '';
  const status = error?.http_status;
  const details = [
    endpoint ? `${method ? `${method} ` : ''}${endpoint}` : '',
    typeof status === 'number' && Number.isInteger(status) && status >= 100 && status <= 599
      ? `HTTP ${status}`
      : '',
  ].filter(Boolean);
  return [message, ...details].join(' · ');
}

/** A connectivity check reports an observed release; it does not verify a template contract. */
export function siteConnectionVersionInfo(site: Site | undefined, t: (zh: string, en: string) => string) {
  const observed =
    typeof site?.connection_check?.version === 'string' ? site.connection_check.version.trim() : '';
  if (observed) {
    return {
      summary: observed,
      tooltip: `${t('最近接口验证获取的版本', 'Version observed during the latest connection check')} · ${observed}`,
    };
  }
  const verified = siteVersionInfo(site, t);
  if (!verified) return null;
  return {
    summary: verified.summary,
    tooltip: `${t('已核对的适配信息', 'Previously verified compatibility information')} · ${verified.label}${verified.fingerprint ? `\nSHA-256: ${verified.fingerprint}` : ''}`,
  };
}

/** Keep reviewed build fingerprints distinct from published release versions. */
export function siteVersionInfo(site: Site | undefined, t: (zh: string, en: string) => string) {
  const version = typeof site?.verified_version === 'string' ? site.verified_version.trim() : '';
  if (version) {
    return { summary: version, label: `${t('站点版本', 'Site version')} ${version}`, fingerprint: '' };
  }
  const fingerprint = site?.verified_build?.fingerprint;
  if (typeof fingerprint === 'string' && fingerprint.length === 64 && /^[a-fA-F0-9]{64}$/.test(fingerprint)) {
    const normalized = fingerprint.toLowerCase();
    const label = `${t('已核对构建', 'Verified build')} · ${normalized.slice(0, 12)}`;
    return { summary: label, label, fingerprint: normalized };
  }
  return null;
}

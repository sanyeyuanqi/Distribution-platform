import type { Row } from './core';

export const AWS_BEDROCK_FORMAT = 'newapi-33-aws-bedrock-v1';
export const AWS_CLAUDE_FORMAT = 'newapi-14-aws-claude-v1';
const legacyBedrockFormats = new Set(['newapi-33-aws-ak-sk-v1', 'newapi-33-aws-api-key-v1']);

export function awsFormats(formats: Row[]) {
  const unique = [...new Map(formats.map((format) => [format.id, format])).values()];
  return unique;
}

export function awsCredentialKind(format?: Row) {
  if (format?.schema_config?.type) return format.schema_config.type;
  if (format?.code === AWS_CLAUDE_FORMAT) return 'aws_claude';
  if (format?.code === AWS_BEDROCK_FORMAT || legacyBedrockFormats.has(format?.code || ''))
    return 'aws_bedrock';
  return '';
}

export function awsUploadFormats(formats: Row[]) {
  const unique = awsFormats(formats);
  const hasMerged = unique.some(
    (format) =>
      format.enabled !== false &&
      awsCredentialKind(format) === 'aws_bedrock' &&
      !legacyBedrockFormats.has(format.code),
  );
  return hasMerged
    ? unique.filter(
        (format) => !legacyBedrockFormats.has(format.code) || (format.version && format.version !== '1'),
      )
    : unique;
}

export function apiAwsUrlError(value: string, required = true) {
  const address = value.trim();
  if (!address) return required ? '请填写 Claude 代理的 API 地址（Base URL）。' : '';
  try {
    const url = new URL(address);
    const host = url.hostname;
    const labels = host.split('.');
    const reserved = new Set(['xxx', 'example', 'test', 'tenant', 'your-tenant', 'localhost', 'placeholder']);
    if (/^(bedrock-mantle|bedrock-runtime|aws-external-anthropic)\./.test(host))
      return '此 AWS 官方服务需要专用路径或工作区配置，请填写 Anthropic 兼容租户代理地址。';
    if (
      address.length > 1000 ||
      !/^https:\/\/[a-z0-9.-]+\/?$/i.test(address) ||
      /\s|[\u0000-\u001f]/.test(address) ||
      url.protocol !== 'https:' ||
      !host.endsWith('.api.aws') ||
      labels.length < 3 ||
      host.length > 253 ||
      labels.some((label) => !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label)) ||
      labels.slice(0, -2).some((label) => reserved.has(label)) ||
      url.username ||
      url.password ||
      url.port ||
      address
        .slice(address.indexOf('://') + 3)
        .split('/')[0]
        .includes(':') ||
      !['', '/'].includes(url.pathname) ||
      address.includes('?') ||
      address.includes('#') ||
      address.includes('\\')
    )
      return '请填写真实的 HTTPS *.api.aws 地址，不含端口、路径、账号或查询参数。';
  } catch {
    return '请填写有效的 HTTPS *.api.aws 地址。';
  }
  return '';
}

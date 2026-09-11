import { useState } from 'react';
import type { FormEvent } from 'react';
import { AlertCircle, Eye, EyeOff, Languages, LoaderCircle, LockKeyhole, UserRound } from 'lucide-react';
import { api, useApp } from '../core';
import Tooltip from '../components/Tooltip';

export default function Login() {
  const { t, lang, toggleLang, setUser } = useApp();
  const [form, setForm] = useState({ username: '', password: '' });
  const [showPassword, setShowPassword] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (loading) return;
    setError('');
    setLoading(true);
    try {
      const result = await api('/auth/login', 'POST', form);
      setUser(result.user);
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : t('登录失败，请重试', 'Unable to sign in. Please try again.'),
      );
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="login-scene">
      <div className="login-tools">
        <Tooltip content={lang === 'zh' ? 'English' : '简体中文'}>
          <button
            type="button"
            className="login-language"
            onClick={toggleLang}
            aria-label={lang === 'zh' ? 'Switch to English' : '切换为简体中文'}
          >
            <Languages size={18} strokeWidth={1.7} />
            <span>{lang === 'zh' ? '中' : 'EN'}</span>
          </button>
        </Tooltip>
      </div>

      <header className="login-intro">
        <p className="login-eyebrow">KEYACROSS CONSOLE</p>
        <h1>{t('渠道、密钥与用量', 'Channels, keys & usage')}</h1>
        <p className="login-description">
          {t('让多平台渠道管理更简单', 'A simpler way to manage channels across platforms.')}
        </p>
        <div className="login-tags" aria-label={t('平台功能', 'Platform features')}>
          <span>{t('渠道管理', 'Channels')}</span>
          <span>{t('密钥上传', 'Key uploads')}</span>
          <span>{t('用量查看', 'Usage insights')}</span>
        </div>
      </header>

      <section className="login-panel" aria-labelledby="login-heading">
        <h2 id="login-heading">{t('KeyAcross 系统', 'KeyAcross Console')}</h2>
        <form className="login-form" onSubmit={submit} aria-busy={loading}>
          <div className="login-field">
            <label htmlFor="login-username">{t('用户名', 'Username')}</label>
            <div className="login-input-wrap">
              <UserRound className="login-field-icon" size={17} strokeWidth={1.7} aria-hidden="true" />
              <input
                id="login-username"
                name="username"
                autoComplete="username"
                autoCapitalize="none"
                spellCheck={false}
                required
                disabled={loading}
                placeholder={t('用户名', 'Username')}
                value={form.username}
                onChange={(event) => setForm((previous) => ({ ...previous, username: event.target.value }))}
              />
            </div>
          </div>
          <div className="login-field">
            <label htmlFor="login-password">{t('密码', 'Password')}</label>
            <div className="login-input-wrap login-password-wrap">
              <LockKeyhole className="login-field-icon" size={17} strokeWidth={1.7} aria-hidden="true" />
              <input
                id="login-password"
                name="password"
                type={showPassword ? 'text' : 'password'}
                autoComplete="current-password"
                required
                disabled={loading}
                placeholder={t('密码', 'Password')}
                value={form.password}
                onChange={(event) => setForm((previous) => ({ ...previous, password: event.target.value }))}
              />
              <button
                type="button"
                className="login-password-toggle"
                onClick={() => setShowPassword((previous) => !previous)}
                aria-label={showPassword ? t('隐藏密码', 'Hide password') : t('显示密码', 'Show password')}
                aria-pressed={showPassword}
              >
                {showPassword ? <Eye size={17} strokeWidth={1.7} /> : <EyeOff size={17} strokeWidth={1.7} />}
              </button>
            </div>
          </div>
          {error && (
            <div className="login-error" role="alert">
              <AlertCircle size={16} aria-hidden="true" />
              <span>{error}</span>
            </div>
          )}
          <button type="submit" className="login-submit" disabled={loading}>
            {loading && <LoaderCircle className="login-loading" size={16} aria-hidden="true" />}
            {loading ? t('正在登录…', 'Signing in…') : t('登 录', 'Sign in')}
          </button>
        </form>
      </section>

      <div className="login-feature-cards" aria-label={t('工作空间概览', 'Workspace overview')}>
        <div>
          <span>{t('渠道', 'Channels')}</span>
          <strong>{t('集中管理', 'All in one place')}</strong>
        </div>
        <div>
          <span>{t('密钥', 'Keys')}</span>
          <strong>{t('统一入口', 'One workspace')}</strong>
        </div>
        <div>
          <span>{t('用量', 'Usage')}</span>
          <strong>{t('清晰可见', 'Clear insights')}</strong>
        </div>
      </div>

      <div className="login-server-art" aria-hidden="true">
        <i />
        <i />
        <i />
      </div>
      <div className="login-orbit-art" aria-hidden="true">
        <i />
        <i />
        <i />
        <b />
        <b />
        <b />
      </div>
    </main>
  );
}

import { useState } from 'react';
import type { FormEvent } from 'react';
import { Check, LoaderCircle } from 'lucide-react';
import { api, Field, Modal, Notice, useAction, useApp } from '../core';
import Select from './Select';

export default function CreateUserDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const { user, t, notify } = useApp();
  const [role, setRole] = useState<'user' | 'admin'>('user');
  const [username, setUsername] = useState('');
  const [nickname, setNickname] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const { busy, run } = useAction();
  const changeRole = (next: 'user' | 'admin') => {
    setRole(next);
    setError('');
  };
  const close = () => {
    if (!busy) onClose();
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (busy || user?.role !== 'superadmin') return;
    setError('');
    if (username.length < 3 || username.length > 64 || /[^a-zA-Z0-9_.-]/.test(username)) {
      setError(
        t(
          '用户名须为 3–64 位，仅支持字母、数字、下划线、点和短横线。',
          'Use 3–64 letters, numbers, underscores, dots or hyphens for the username.',
        ),
      );
      return;
    }
    if (!nickname.trim() || Array.from(nickname).length > 100) {
      setError(t('昵称须为 1–100 个字符。', 'Display name must contain 1–100 characters.'));
      return;
    }
    const passwordLength = Array.from(password).length;
    if (passwordLength < 6 || passwordLength > 256) {
      setError(t('密码须为 6–256 个字符。', 'Password must contain 6–256 characters.'));
      return;
    }
    const payload = {
      username,
      nickname,
      password,
      role,
    };
    void run(async () => {
      try {
        await api('/users', 'POST', payload);
        notify(t('账号已创建', 'Account created'));
        onCreated();
        onClose();
      } catch (reason) {
        setError(
          reason instanceof TypeError
            ? t('无法连接服务，请检查网络后重试。', 'Could not connect. Check your network and retry.')
            : reason instanceof Error
              ? reason.message
              : t('创建失败，请重试。', 'Creation failed. Retry.'),
        );
      }
    });
  };
  return (
    <Modal title={t('新建用户', 'Create user')} onClose={close}>
      <form className="form-stack" onSubmit={submit}>
        {error && <Notice kind="error">{error}</Notice>}
        <Field label={t('账号类型', 'Account type')}>
          <Select
            value={role}
            aria-label={t('账号类型', 'Account type')}
            disabled={busy}
            onChange={(event) => changeRole(event.target.value === 'admin' ? 'admin' : 'user')}
          >
            <option value="user">{t('普通用户', 'Member')}</option>
            <option value="admin">{t('管理员', 'Administrator')}</option>
          </Select>
        </Field>
        <Field
          label={t('用户名', 'Username')}
          hint={t(
            '3–64 位字母、数字、下划线、点或短横线。',
            '3–64 letters, numbers, underscores, dots or hyphens.',
          )}
        >
          <input
            aria-label={t('用户名', 'Username')}
            value={username}
            required
            disabled={busy}
            autoComplete="username"
            onChange={(event) => setUsername(event.target.value)}
          />
        </Field>
        <Field label={t('昵称', 'Display name')}>
          <input
            aria-label={t('昵称', 'Display name')}
            value={nickname}
            required
            disabled={busy}
            onChange={(event) => setNickname(event.target.value)}
          />
        </Field>
        <Field
          label={t('初始密码', 'Initial password')}
          hint={t('密码须为 6–256 个字符。', 'Password must contain 6–256 characters.')}
        >
          <input
            aria-label={t('初始密码', 'Initial password')}
            type="password"
            value={password}
            required
            disabled={busy}
            autoComplete="new-password"
            onChange={(event) => setPassword(event.target.value)}
          />
        </Field>
        <div className="form-actions">
          <button type="button" className="button secondary" disabled={busy} onClick={close}>
            {t('取消', 'Cancel')}
          </button>
          <button className="button" disabled={busy}>
            {busy ? <LoaderCircle size={16} className="spin" /> : <Check size={16} />}
            {t('保存', 'Save')}
          </button>
        </div>
      </form>
    </Modal>
  );
}

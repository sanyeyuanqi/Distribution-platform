import { useCallback, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { createPortal } from 'react-dom';
import * as Popover from '@radix-ui/react-popover';
import {
  Check,
  ChevronDown,
  CircleUserRound,
  LoaderCircle,
  LockKeyhole,
  LogOut,
  UserRound,
} from 'lucide-react';
import { api, Field, Modal, Notice, RecordForm, useApp } from '../core';
import './account-menu.css';

function ChangePasswordDialog({ onClose }: { onClose: () => void }) {
  const { t, setUser, notify } = useApp();
  const [currentPassword, setCurrentPassword] = useState('');
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const pending = useRef(false);
  const close = useCallback(() => {
    if (!pending.current) onClose();
  }, [onClose]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending.current) return;
    setError('');
    if (!currentPassword || Array.from(currentPassword).length > 256) {
      setError(
        t('请输入有效的当前密码，最多 256 个字符。', 'Enter your current password, up to 256 characters.'),
      );
      return;
    }
    const length = Array.from(password).length;
    if (length < 6 || length > 256) {
      setError(t('新密码须为 6–256 个字符。', 'New password must contain 6–256 characters.'));
      return;
    }
    if (password !== confirmation) {
      setError(t('两次输入的新密码不一致。', 'The new passwords do not match.'));
      return;
    }
    pending.current = true;
    setBusy(true);
    try {
      const result = await api('/auth/profile', 'PATCH', { current_password: currentPassword, password });
      setUser(result.user);
      notify(t('密码已修改', 'Password changed'));
      onClose();
    } catch (reason) {
      setError(
        reason instanceof TypeError
          ? t('无法连接服务，请稍后重试。', 'Could not connect. Please retry.')
          : reason instanceof Error
            ? reason.message
            : t('修改失败，请重试。', 'Could not change the password. Retry.'),
      );
    } finally {
      pending.current = false;
      setBusy(false);
    }
  };

  return (
    <Modal title={t('修改密码', 'Change password')} onClose={close}>
      <form className="form-stack" onSubmit={submit}>
        {error && <Notice kind="error">{error}</Notice>}
        <Field label={t('当前密码', 'Current password')}>
          <input
            type="password"
            name="current_password"
            autoComplete="current-password"
            required
            disabled={busy}
            value={currentPassword}
            onChange={(event) => setCurrentPassword(event.target.value)}
          />
        </Field>
        <Field label={t('新密码', 'New password')} hint={t('6–256 个字符', '6–256 characters')}>
          <input
            type="password"
            name="password"
            autoComplete="new-password"
            required
            disabled={busy}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </Field>
        <Field label={t('确认新密码', 'Confirm new password')}>
          <input
            type="password"
            name="password_confirmation"
            autoComplete="new-password"
            required
            disabled={busy}
            value={confirmation}
            onChange={(event) => setConfirmation(event.target.value)}
          />
        </Field>
        <div className="form-actions">
          <button type="button" className="button secondary" disabled={busy} onClick={close}>
            {t('取消', 'Cancel')}
          </button>
          <button type="submit" className="button" disabled={busy}>
            {busy ? <LoaderCircle size={16} className="spin" /> : <Check size={16} />}
            {t('保存', 'Save')}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export default function AccountMenu({
  onSignOut,
  signingOut,
}: {
  onSignOut: () => Promise<void>;
  signingOut: boolean;
}) {
  const { user, t, setUser, notify } = useApp();
  const [open, setOpen] = useState(false);
  const [dialog, setDialog] = useState<'profile' | 'password' | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const openingDialog = useRef(false);
  const showDialog = (next: 'profile' | 'password') => {
    openingDialog.current = true;
    trigger.current?.focus();
    setOpen(false);
    setDialog(next);
  };
  const closeDialog = useCallback(() => {
    setDialog(null);
    trigger.current?.focus();
  }, []);

  return (
    <>
      <Popover.Root open={open} onOpenChange={setOpen}>
        <Popover.Trigger asChild>
          <button
            ref={trigger}
            type="button"
            className="user-menu"
            aria-label={`${t('账号菜单', 'Account menu')}: ${user?.username}`}
          >
            <CircleUserRound size={20} strokeWidth={1.7} aria-hidden="true" />
            <strong>{user?.username}</strong>
            <ChevronDown className="user-menu-chevron" size={14} aria-hidden="true" />
          </button>
        </Popover.Trigger>
        <Popover.Portal>
          <Popover.Content
            className="account-menu-popover"
            align="end"
            sideOffset={10}
            collisionPadding={12}
            aria-label={t('账号菜单', 'Account menu')}
            onCloseAutoFocus={(event) => {
              if (openingDialog.current) {
                event.preventDefault();
                openingDialog.current = false;
              }
            }}
          >
            <button type="button" onClick={() => showDialog('profile')}>
              <UserRound size={16} aria-hidden="true" />
              {t('个人资料', 'Your profile')}
            </button>
            <button type="button" onClick={() => showDialog('password')}>
              <LockKeyhole size={16} aria-hidden="true" />
              {t('修改密码', 'Change password')}
            </button>
            <div className="account-menu-divider" />
            <button
              type="button"
              className="account-menu-signout"
              disabled={signingOut}
              onClick={() => {
                setOpen(false);
                void onSignOut();
              }}
            >
              <LogOut size={16} aria-hidden="true" />
              {t('退出登录', 'Sign out')}
            </button>
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
      {dialog === 'profile' &&
        createPortal(
          <RecordForm
            title={t('个人资料', 'Your profile')}
            initial={user!}
            onClose={closeDialog}
            fields={[{ name: 'nickname', label: t('昵称', 'Display name'), required: true, maxLength: 100 }]}
            onSave={async (values) => {
              const result = await api('/auth/profile', 'PATCH', { nickname: values.nickname });
              setUser(result.user);
              notify(t('资料已更新', 'Profile updated'));
            }}
          />,
          document.body,
        )}
      {dialog === 'password' && createPortal(<ChangePasswordDialog onClose={closeDialog} />, document.body)}
    </>
  );
}

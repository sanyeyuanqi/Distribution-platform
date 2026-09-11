import { FileClock, ListTodo } from 'lucide-react';
import type { ReactNode } from 'react';
import { Navigate, NavLink } from 'react-router-dom';
import { useApp } from '../core';
import { canAccessSection, sectionRoles } from '../permissions';
import './system-settings.css';

const sections = [
  { section: 'tasks', icon: ListTodo, zh: '任务中心', en: 'Task center' },
  { section: 'audit', icon: FileClock, zh: '操作审计', en: 'Audit log' },
] as const;

export const systemSettingsRoles = [...new Set([...sectionRoles.tasks, ...sectionRoles.audit])];

export function SystemSettingsIndex() {
  const { user } = useApp();
  const first = sections.find(({ section }) => canAccessSection(user?.role, section));
  return <Navigate to={first ? `/settings/${first.section}` : '/'} replace />;
}

export default function SystemSettingsHeader({ actions }: { actions: ReactNode }) {
  const { user, t } = useApp();
  return (
    <div className="system-settings-toolbar">
      <nav className="system-settings-links" aria-label={t('系统设置', 'System settings')}>
        {sections
          .filter(({ section }) => canAccessSection(user?.role, section))
          .map(({ section, icon: Icon, zh, en }) => (
            <NavLink key={section} to={`/settings/${section}`} end>
              <Icon size={16} />
              <span>{t(zh, en)}</span>
            </NavLink>
          ))}
      </nav>
      <div className="actions">{actions}</div>
    </div>
  );
}

import { Component, lazy, Suspense, useState } from 'react';
import type { ReactNode } from 'react';
import { Navigate, NavLink, Outlet, Route, Routes, useLocation } from 'react-router-dom';
import {
  Bell,
  BookOpen,
  Boxes,
  CircleDollarSign,
  Globe2,
  KeyRound,
  LayoutDashboard,
  Layers3,
  Languages,
  Menu,
  Network,
  PanelLeftClose,
  PanelLeftOpen,
  Power,
  Settings,
  Upload,
  Users,
} from 'lucide-react';
import { api, Loading, useAction, useApp } from './core';
import Login from './pages/Login';
import { sectionRoles } from './permissions';
import Tooltip from './components/Tooltip';
import AnnouncementNotifications from './components/AnnouncementNotifications';
import AccountMenu from './components/AccountMenu';
import { SystemSettingsIndex, systemSettingsRoles } from './pages/SystemSettings';

const Dashboard = lazy(() => import('./pages/Dashboard'));
const ModelGaps = lazy(() => import('./pages/ModelGaps'));
const SimpleUpload = lazy(() => import('./pages/SimpleUpload'));
const UploadTemplates = lazy(() => import('./pages/UploadTemplates'));
const SettlementOrders = lazy(() => import('./pages/SettlementOrders'));
const Channels = lazy(() => import('./pages/Channels').then((page) => ({ default: page.Channels })));
const Tasks = lazy(() => import('./pages/Channels').then((page) => ({ default: page.Tasks })));
const UsersPage = lazy(() => import('./pages/Management').then((page) => ({ default: page.UsersPage })));
const Sites = lazy(() => import('./pages/Management').then((page) => ({ default: page.Sites })));
const Announcements = lazy(() =>
  import('./pages/Management').then((page) => ({ default: page.Announcements })),
);
const Audit = lazy(() => import('./pages/Management').then((page) => ({ default: page.Audit })));

class PageBoundary extends Component<{ children: ReactNode; fallback: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}

const menu = [
  {
    path: '/model-gaps',
    icon: Boxes,
    zh: '模型缺口',
    en: 'Model gaps',
    roles: ['superadmin', 'admin', 'user'],
  },
  { path: '/', icon: LayoutDashboard, zh: '控制台', en: 'Overview', roles: ['superadmin', 'admin', 'user'] },
  { path: '/upload', icon: Upload, zh: '上传密钥', en: 'Upload keys', roles: ['admin', 'user'] },
  { path: '/users', icon: Users, zh: '子账号管理', en: 'Subaccounts', roles: ['superadmin', 'admin'] },
  { path: '/sites', icon: Globe2, zh: '站点管理', en: 'Sites', roles: ['superadmin'] },
  {
    path: '/upload-templates',
    icon: Layers3,
    zh: '分发模板',
    en: 'Distribution templates',
    roles: ['superadmin'],
  },
  {
    path: '/channels',
    icon: KeyRound,
    zh: '我的渠道',
    en: 'My channels',
    roles: ['superadmin', 'admin', 'user'],
  },
  {
    path: '/settlements',
    icon: CircleDollarSign,
    zh: '结算历史',
    en: 'Settlement history',
    roles: ['superadmin', 'admin', 'user'],
  },
  {
    path: '/announcements',
    icon: BookOpen,
    zh: '公告中心',
    en: 'Announcements',
    roles: sectionRoles.announcements,
  },
  { path: '/settings', icon: Settings, zh: '系统设置', en: 'System settings', roles: systemSettingsRoles },
];
function Shell() {
  const { user, t, lang, toggleLang, setUser } = useApp();
  const [collapsed, setCollapsed] = useState(false);
  const [mobile, setMobile] = useState(false);
  const { busy: signingOut, run: runSignOut } = useAction();
  const location = useLocation();
  const canReadAnnouncements = !!user;
  const [unread, setUnread] = useState(0);
  const [announcementOpenRequest, openAnnouncements] = useState(0);
  const signOut = async () => {
    await runSignOut(async () => {
      await api('/auth/logout', 'POST');
      setUser(null);
    });
  };
  const current = menu.find(
    (m) => m.path === location.pathname || (m.path !== '/' && location.pathname.startsWith(`${m.path}/`)),
  );
  const navName = (m: (typeof menu)[number]) =>
    user?.role === 'superadmin' && m.path === '/users'
      ? t('用户管理', 'Users')
      : user?.role === 'superadmin' && m.path === '/channels'
        ? t('渠道管理', 'Channels')
        : t(m.zh, m.en);
  return (
    <div className={`app-shell ${collapsed ? 'collapsed' : ''}`}>
      <aside
        id="workspace-navigation"
        aria-label={t('主导航', 'Main navigation')}
        className={`sidebar ${mobile ? 'mobile-open' : ''}`}
      >
        <nav>
          {menu
            .filter((m) => m.roles.includes(user!.role))
            .map((m) => (
              <Tooltip key={m.path} content={collapsed && !mobile ? navName(m) : undefined} side="right">
                <NavLink
                  end={m.path === '/'}
                  to={m.path}
                  aria-label={navName(m)}
                  onClick={() => setMobile(false)}
                >
                  <m.icon size={18} strokeWidth={1.8} aria-hidden="true" />
                  <span>{navName(m)}</span>
                </NavLink>
              </Tooltip>
            ))}
        </nav>
        <div className="sidebar-bottom">
          <Tooltip content={collapsed && !mobile ? t('退出登录', 'Sign out') : undefined} side="right">
            <button
              type="button"
              className="sidebar-logout"
              aria-label={t('退出登录', 'Sign out')}
              disabled={signingOut}
              onClick={() => void signOut()}
            >
              <Power size={18} strokeWidth={1.8} aria-hidden="true" />
              <span>{t('退出登录', 'Sign out')}</span>
            </button>
          </Tooltip>
        </div>
      </aside>
      {mobile && <div className="sidebar-scrim" onClick={() => setMobile(false)} />}
      <div
        className={`main-shell${location.pathname === '/upload' ? ' upload-page-shell' : ''}${location.pathname === '/upload-templates' ? ' templates-page-shell' : ''}${location.pathname.startsWith('/settings/') ? ' settings-page-shell' : ''}`}
      >
        <header className="topbar">
          <div className="breadcrumb">
            <Tooltip
              content={collapsed ? t('展开导航', 'Expand navigation') : t('收起导航', 'Collapse navigation')}
            >
              <button
                type="button"
                className="icon-button desktop-navigation-toggle"
                aria-label={
                  collapsed ? t('展开导航', 'Expand navigation') : t('收起导航', 'Collapse navigation')
                }
                aria-expanded={!collapsed}
                aria-controls="workspace-navigation"
                onClick={() => setCollapsed((value) => !value)}
              >
                {collapsed ? (
                  <PanelLeftOpen size={20} strokeWidth={1.8} aria-hidden="true" />
                ) : (
                  <PanelLeftClose size={20} strokeWidth={1.8} aria-hidden="true" />
                )}
              </button>
            </Tooltip>
            <button
              type="button"
              className="icon-button mobile-toggle"
              aria-label={mobile ? t('收起导航', 'Close navigation') : t('打开导航', 'Open navigation')}
              aria-expanded={mobile}
              aria-controls="workspace-navigation"
              onClick={() => setMobile(!mobile)}
            >
              <Menu size={20} strokeWidth={1.8} aria-hidden="true" />
            </button>
            <strong className="mobile-page-name">
              {current ? navName(current) : t('工作台', 'Workspace')}
            </strong>
          </div>
          <div className="topbar-actions">
            {canReadAnnouncements && (
              <button
                className="icon-button notification-button"
                aria-label={t('公告', 'Announcements')}
                aria-haspopup="dialog"
                onClick={() => openAnnouncements((request) => request + 1)}
              >
                <Bell size={19} />
                {unread > 0 && <i>{unread > 9 ? '9+' : unread}</i>}
              </button>
            )}
            <Tooltip content={lang === 'zh' ? 'English' : '简体中文'}>
              <button
                className="icon-button language-toggle"
                onClick={toggleLang}
                aria-label={lang === 'zh' ? 'Switch to English' : '切换为简体中文'}
              >
                <Languages size={19} />
              </button>
            </Tooltip>
            <AccountMenu onSignOut={signOut} signingOut={signingOut} />
          </div>
        </header>
        <main className="page-container">
          <PageBoundary
            key={`${user?.id}:${location.pathname}`}
            fallback={
              <div className="state-block" role="alert">
                <strong>
                  {t('页面加载失败，请刷新后重试。', 'The page could not load. Refresh to retry.')}
                </strong>
                <button className="button" onClick={() => window.location.reload()}>
                  {t('刷新页面', 'Reload page')}
                </button>
              </div>
            }
          >
            <Suspense fallback={<Loading />}>
              <Outlet />
            </Suspense>
          </PageBoundary>
        </main>
      </div>
      {canReadAnnouncements && (
        <AnnouncementNotifications
          key={`${user!.id}:${user!.role}`}
          onUnreadChange={setUnread}
          openRequest={announcementOpenRequest}
        />
      )}
    </div>
  );
}
function Guard({ roles }: { roles: readonly string[] }) {
  const { user } = useApp();
  return roles.includes(user!.role) ? <Outlet /> : <Navigate to="/" replace />;
}
function SettlementHistoryRedirect() {
  const { search, hash } = useLocation();
  return <Navigate to={{ pathname: '/settlements', search, hash }} replace />;
}
function SystemSectionRedirect({ section }: { section: 'tasks' | 'audit' }) {
  const { search, hash } = useLocation();
  return <Navigate to={{ pathname: `/settings/${section}`, search, hash }} replace />;
}
export default function App() {
  const { user, ready } = useApp();
  if (!ready)
    return (
      <div className="boot-screen">
        <Network size={34} />
        <Loading />
      </div>
    );
  if (!user) return <Login />;
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<Dashboard />} />
        <Route path="channels" element={<Channels />} />
        <Route path="usage" element={<Navigate to="/channels" replace />} />
        <Route path="model-gaps" element={<ModelGaps />} />
        <Route element={<Guard roles={sectionRoles.tasks} />}>
          <Route path="tasks" element={<SystemSectionRedirect section="tasks" />} />
        </Route>
        <Route element={<Guard roles={sectionRoles.announcements} />}>
          <Route path="announcements" element={<Announcements />} />
        </Route>
        <Route element={<Guard roles={sectionRoles.audit} />}>
          <Route path="audit" element={<SystemSectionRedirect section="audit" />} />
        </Route>
        <Route element={<Guard roles={systemSettingsRoles} />}>
          <Route path="settings">
            <Route index element={<SystemSettingsIndex />} />
            <Route element={<Guard roles={sectionRoles.tasks} />}>
              <Route path="tasks" element={<Tasks />} />
            </Route>
            <Route element={<Guard roles={sectionRoles.audit} />}>
              <Route path="audit" element={<Audit />} />
            </Route>
          </Route>
        </Route>
        <Route path="bills" element={<Navigate to="/settlements" replace />} />
        <Route path="settlements" element={<SettlementOrders />} />
        <Route path="settlement-orders" element={<SettlementHistoryRedirect />} />
        <Route element={<Guard roles={['admin', 'user']} />}>
          <Route path="upload" element={<SimpleUpload />} />
          <Route path="upload/advanced" element={<Navigate to="/upload" replace />} />
        </Route>
        <Route element={<Guard roles={['superadmin', 'admin']} />}>
          <Route path="users" element={<UsersPage />} />
        </Route>
        <Route element={<Guard roles={['superadmin']} />}>
          <Route path="sites" element={<Sites />} />
          <Route path="catalog" element={<Navigate to="/upload-templates" replace />} />
          <Route path="upload-templates" element={<UploadTemplates />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

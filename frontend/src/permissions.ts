import type { User } from './core';

export type RestrictedSection = 'announcements' | 'tasks' | 'audit';

export const sectionRoles: Record<RestrictedSection, readonly User['role'][]> = {
  announcements: ['superadmin'],
  tasks: ['superadmin', 'user'],
  audit: ['superadmin'],
};

export function canAccessSection(role: User['role'] | null | undefined, section: RestrictedSection): boolean {
  return role !== null && role !== undefined && sectionRoles[section].includes(role);
}

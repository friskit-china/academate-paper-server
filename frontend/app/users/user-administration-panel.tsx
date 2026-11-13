"use client";

import { type FormEvent, useEffect, useState } from "react";

import {
  ApiRequestError,
  createInvitation,
  disableManagedUser,
  getWebSessionStatus,
  listInvitations,
  listManagedUsers,
  revokeInvitation,
  type InvitationStatus,
  type InvitationSummary,
  type ManagedUser,
} from "../api";
import { isCanonicalRegistrationUrl } from "./invitation-url";

const INVITATION_STATUS_LABELS: Record<InvitationStatus, string> = {
  ACTIVE: "可使用",
  EXPIRED: "已过期",
  REDEEMED: "已兑换",
  REVOKED: "已撤销",
};

const EXPIRATION_OPTIONS = [
  { seconds: 24 * 60 * 60, label: "1 天" },
  { seconds: 7 * 24 * 60 * 60, label: "7 天" },
  { seconds: 30 * 24 * 60 * 60, label: "30 天" },
  { seconds: 90 * 24 * 60 * 60, label: "90 天" },
] as const;

function administrationError(error: unknown): string {
  if (!(error instanceof ApiRequestError)) return "暂时无法连接管理服务，请稍后重试。";
  if (error.status === 401) return "登录会话已失效，请重新登录。";
  if (error.status === 403) return "当前账号或安装模式不允许执行这项管理操作。";
  if (error.status === 404) return "目标记录不属于当前文库，或已经不可用。";
  if (error.status === 409) return "记录状态已经变化，或该受保护账号不能停用。";
  if (error.status === 422) return "提交内容无效，请检查有效期和备注后重试。";
  return "管理操作失败，请稍后重试。";
}

function formatDate(value: string | null): string {
  if (value === null) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function redeemedBy(invitation: InvitationSummary): string {
  if (invitation.redeemed_by_user_id === null) return "—";
  const displayName = invitation.redeemed_by_display_name?.trim();
  const username = invitation.redeemed_by_username?.trim();
  if (displayName && username) return `${displayName}（@${username}）`;
  return displayName || (username ? `@${username}` : "已注册成员");
}

export function UserAdministrationPanel() {
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [invitations, setInvitations] = useState<InvitationSummary[]>([]);
  const [canManageUsers, setCanManageUsers] = useState(false);
  const [canManageInvitations, setCanManageInvitations] = useState(false);
  const [authorizationResolved, setAuthorizationResolved] = useState(false);
  const [expiresInSeconds, setExpiresInSeconds] = useState(7 * 24 * 60 * 60);
  const [label, setLabel] = useState("");
  const [oneTimeRegistrationUrl, setOneTimeRegistrationUrl] = useState<string | null>(null);
  const [copyMessage, setCopyMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getWebSessionStatus()
      .then(async (session) => {
        if (cancelled) return;
        setCanManageUsers(session.can_manage_users);
        setCanManageInvitations(session.can_manage_invitations);
        setAuthorizationResolved(true);
        const [nextUsers, nextInvitations] = await Promise.all([
          session.can_manage_users ? listManagedUsers() : Promise.resolve([]),
          session.can_manage_invitations ? listInvitations(true) : Promise.resolve([]),
        ]);
        if (cancelled) return;
        setUsers(nextUsers);
        setInvitations(nextInvitations);
        setError(null);
      })
      .catch((caught) => {
        if (!cancelled) setError(administrationError(caught));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function submitInvitation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting || !canManageInvitations) return;
    setSubmitting(true);
    setError(null);
    setCopyMessage(null);
    setOneTimeRegistrationUrl(null);
    try {
      const created = await createInvitation({
        expires_in_seconds: expiresInSeconds,
        ...(label.trim().length === 0 ? {} : { label: label.trim() }),
      });
      const { registration_url: registrationUrl, ...summary } = created;
      setInvitations((current) => [summary, ...current.filter((item) => item.id !== summary.id)]);
      setLabel("");
      if (!isCanonicalRegistrationUrl(registrationUrl, window.location.origin)) {
        setError("邀请码已经创建，但注册链接响应不安全，因而没有显示。请从列表撤销后重试。");
        return;
      }
      setOneTimeRegistrationUrl(registrationUrl);
    } catch (caught) {
      setError(administrationError(caught));
    } finally {
      setSubmitting(false);
    }
  }

  async function copyRegistrationUrl() {
    if (oneTimeRegistrationUrl === null) return;
    try {
      await navigator.clipboard.writeText(oneTimeRegistrationUrl);
      setCopyMessage("注册链接已复制。请通过可信渠道发给受邀成员。");
    } catch {
      setCopyMessage("浏览器未允许自动复制，请手动选择并复制完整链接。");
    }
  }

  async function revoke(invitation: InvitationSummary) {
    if (submitting || invitation.status !== "ACTIVE" || !canManageInvitations) return;
    if (!window.confirm(`确定撤销邀请码“${invitation.masked_hint}”吗？撤销后无法恢复。`)) return;
    setSubmitting(true);
    setError(null);
    try {
      const revoked = await revokeInvitation(invitation.id);
      setInvitations((current) => current.map((item) => (
        item.id === revoked.id ? revoked : item
      )));
    } catch (caught) {
      setError(administrationError(caught));
    } finally {
      setSubmitting(false);
    }
  }

  async function disable(user: ManagedUser) {
    if (submitting || !canManageUsers || user.is_installation_owner || user.status === "DISABLED") {
      return;
    }
    if (!window.confirm(`确定停用“${user.display_name}（${user.username}）”吗？其现有会话会立即失效。`)) {
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const disabled = await disableManagedUser(user.id);
      setUsers((current) => current.map((item) => (
        item.id === disabled.id ? disabled : item
      )));
    } catch (caught) {
      setError(administrationError(caught));
    } finally {
      setSubmitting(false);
    }
  }

  if (loading) return <p className="setup-message" role="status">正在读取管理权限和列表…</p>;
  if (authorizationResolved && !canManageUsers && !canManageInvitations) {
    return (
      <section className="settings-card administration-denied" role="alert">
        <h2>没有管理权限</h2>
        <p>当前账号不能查看或管理成员与邀请码。</p>
      </section>
    );
  }

  return (
    <div className="user-admin-stack">
      {canManageInvitations ? (
        <section className="settings-card invitation-create-card">
          <h2>创建单次邀请码</h2>
          <p>新成员会固定加入当前文库并成为普通成员。完整注册链接只在创建响应中显示一次。</p>
          <form className="user-create-form" onSubmit={submitInvitation}>
            <div className="setup-form-grid">
              <label>
                <span>有效期</span>
                <select
                  disabled={submitting}
                  onChange={(event) => setExpiresInSeconds(Number(event.target.value))}
                  value={expiresInSeconds}
                >
                  {EXPIRATION_OPTIONS.map((option) => (
                    <option key={option.seconds} value={option.seconds}>{option.label}</option>
                  ))}
                </select>
              </label>
              <label>
                <span>备注（可选）</span>
                <input
                  autoComplete="off"
                  disabled={submitting}
                  maxLength={128}
                  onChange={(event) => setLabel(event.target.value)}
                  placeholder="例如：研究小组新成员"
                  value={label}
                />
              </label>
            </div>
            <button disabled={submitting} type="submit">
              {submitting ? "正在创建…" : "创建邀请码"}
            </button>
          </form>

          {oneTimeRegistrationUrl !== null ? (
            <aside className="one-time-invitation" aria-live="polite">
              <strong>现在复制完整注册链接</strong>
              <p>关闭或刷新后，系统只能显示脱敏提示，不能恢复这段邀请凭证。</p>
              <div>
                <input
                  aria-label="一次性完整注册链接"
                  onFocus={(event) => event.currentTarget.select()}
                  readOnly
                  value={oneTimeRegistrationUrl}
                />
                <button onClick={() => void copyRegistrationUrl()} type="button">复制链接</button>
              </div>
              {copyMessage !== null ? <small role="status">{copyMessage}</small> : null}
            </aside>
          ) : null}
        </section>
      ) : null}

      {canManageInvitations ? (
        <section className="settings-card invitation-list-card">
          <div className="invitation-section-heading">
            <div>
              <h2>邀请码记录</h2>
              <p>列表只保留脱敏提示和兑换记录，不保存或恢复完整邀请凭证。</p>
            </div>
            <small>{invitations.length} 条</small>
          </div>
          {invitations.length === 0 ? <p>尚未创建邀请码。</p> : null}
          <div className="invitation-list">
            {invitations.map((invitation) => (
              <article key={invitation.id}>
                <div className="invitation-title-row">
                  <div>
                    <strong>{invitation.label || "未命名邀请"}</strong>
                    <code>{invitation.masked_hint}</code>
                  </div>
                  <span className="invitation-status" data-status={invitation.status}>
                    {INVITATION_STATUS_LABELS[invitation.status]}
                  </span>
                </div>
                <dl>
                  <div><dt>创建时间</dt><dd>{formatDate(invitation.created_at)}</dd></div>
                  <div><dt>到期时间</dt><dd>{formatDate(invitation.expires_at)}</dd></div>
                  <div><dt>兑换时间</dt><dd>{formatDate(invitation.redeemed_at)}</dd></div>
                  <div><dt>兑换用户</dt><dd>{redeemedBy(invitation)}</dd></div>
                </dl>
                <div className="invitation-actions">
                  <small>签发渠道：{invitation.issuer_channel === "WEB_ADMIN" ? "网页管理" : "部署命令"}</small>
                  <button
                    disabled={submitting || invitation.status !== "ACTIVE"}
                    onClick={() => void revoke(invitation)}
                    type="button"
                  >
                    {invitation.status === "ACTIVE" ? "撤销" : "不可撤销"}
                  </button>
                </div>
              </article>
            ))}
          </div>
        </section>
      ) : null}

      {canManageUsers ? (
        <section className="settings-card managed-users-card">
          <h2>当前文库用户</h2>
          {users.length === 0 ? <p>当前没有可显示的用户。</p> : null}
          <div className="managed-user-list">
            {users.map((user) => (
              <article key={user.id}>
                <div>
                  <strong>{user.display_name}</strong>
                  <span>@{user.username} · {user.workspace_role}</span>
                </div>
                <span className="managed-user-status" data-status={user.status}>
                  {user.status === "ACTIVE" ? "使用中" : "已停用"}
                </span>
                <button
                  disabled={submitting || user.is_installation_owner || user.status === "DISABLED"}
                  onClick={() => void disable(user)}
                  type="button"
                >
                  {user.is_installation_owner ? "安装所有者" : user.status === "DISABLED" ? "已停用" : "停用"}
                </button>
              </article>
            ))}
          </div>
        </section>
      ) : null}
      {error !== null ? <p className="setup-error administration-error" role="alert">{error}</p> : null}
    </div>
  );
}

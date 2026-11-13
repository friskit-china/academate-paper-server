import type { Metadata } from "next";

import { UserAdministrationPanel } from "./user-administration-panel";

export const metadata: Metadata = { title: "用户管理" };

export default function UsersPage() {
  return (
    <main className="settings-page user-admin-page">
      <header className="page-title">
        <p className="eyebrow">多用户部署</p>
        <h1>成员与邀请</h1>
        <p>创建单次邀请、查看兑换记录，或停用不再需要的成员账号。</p>
      </header>
      <UserAdministrationPanel />
    </main>
  );
}

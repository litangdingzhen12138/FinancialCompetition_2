import type { Metadata } from "next";
import { AppShell } from "../components/AppShell";
import { AdminView } from "../components/AdminView";

export const metadata: Metadata = {
  title: "管理审计",
};

export default function AdminPage() {
  return (
    <AppShell>
      <AdminView />
    </AppShell>
  );
}

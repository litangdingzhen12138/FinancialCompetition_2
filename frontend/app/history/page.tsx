import type { Metadata } from "next";
import { AppShell } from "../components/AppShell";
import { HistoryView } from "../components/HistoryView";

export const metadata: Metadata = {
  title: "历史报告",
};

export default function HistoryPage() {
  return (
    <AppShell>
      <HistoryView />
    </AppShell>
  );
}

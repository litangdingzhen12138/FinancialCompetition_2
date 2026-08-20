import type { Metadata } from "next";
import { HistoryView } from "../../components/HistoryView";

export const metadata: Metadata = {
  title: "历史报告",
};

export default function HistoryPage() {
  return <HistoryView />;
}

import type { Metadata } from "next";
import { ShareView } from "../../components/ShareView";

export const metadata: Metadata = {
  title: "共享报告",
};

export default async function SharedReportPage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  const { token } = await params;
  return <ShareView token={token} />;
}

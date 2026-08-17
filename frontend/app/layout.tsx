import type { Metadata } from "next";
import { headers } from "next/headers";
import "./globals.css";

export async function generateMetadata(): Promise<Metadata> {
  const requestHeaders = await headers();
  const host = requestHeaders.get("host") ?? "localhost:3000";
  const protocol =
    requestHeaders.get("x-forwarded-proto") ??
    (host.startsWith("localhost") || host.startsWith("127.0.0.1")
      ? "http"
      : "https");
  const base = new URL(`${protocol}://${host}`);
  const description =
    "面向银行经营管理的智能问数、可视化分析与业务解释工作台。";
  return {
    metadataBase: base,
    title: {
      default: "数衡 BankInsight",
      template: "%s · 数衡 BankInsight",
    },
    description,
    openGraph: {
      title: "数衡 BankInsight",
      description,
      images: [new URL("/og.png", base).toString()],
      locale: "zh_CN",
      type: "website",
    },
    twitter: {
      card: "summary_large_image",
      title: "数衡 BankInsight",
      description,
      images: [new URL("/og.png", base).toString()],
    },
  };
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}

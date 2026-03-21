import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "LIQ 防猎杀数据大屏",
  description: "加密资产防止损猎杀实时数据大屏",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className="antialiased">{children}</body>
    </html>
  );
}

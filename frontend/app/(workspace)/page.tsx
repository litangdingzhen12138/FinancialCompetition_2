import type { Metadata } from "next";
import { Workbench } from "../components/Workbench";

export const metadata: Metadata = {
  title: "智能问数",
  description: "从自然语言提问到可信数据、动态图表和业务结论的一站式工作台。",
};

export default function Home() {
  return <Workbench />;
}

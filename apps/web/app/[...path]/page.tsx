import { notFound } from "next/navigation";
import Console from "../console";
export default async function Page({params}: {params: Promise<{path: string[]}>}) {
  const {path} = await params;
  const [section, id, subpage] = path;
  if (!["dashboard", "agents", "problem-sources", "problems", "workers", "evaluations"].includes(section)
    || path.length > 3
    || (id && !["agents", "problems", "evaluations"].includes(section))
    || (subpage && (section !== "evaluations" || subpage !== "failure-analysis"))) notFound();
  return <Console />;
}

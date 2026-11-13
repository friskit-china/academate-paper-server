import ArxivImportClient from "./arxiv-import-client";

export default async function ArxivRoutePage({
  params,
}: {
  params: Promise<{ arxivId: string[] }>;
}) {
  const { arxivId } = await params;
  return <ArxivImportClient arxivId={arxivId.join("/")} />;
}

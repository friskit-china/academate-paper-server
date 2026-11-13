import PaperDetailClient from "./paper-detail-client";

export default async function PaperPage({ params }: { params: Promise<{ paperId: string }> }) {
  const { paperId } = await params;
  return <PaperDetailClient paperId={paperId} />;
}

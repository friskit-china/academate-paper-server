"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { importPaper } from "../../api";

export default function ArxivImportClient({ arxivId }: { arxivId: string }) {
  const router = useRouter();
  const started = useRef(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    void importPaper(arxivId)
      .then(({ paper }) => router.replace(`/papers/${paper.id}`))
      .catch((reason) => setError(reason instanceof Error ? reason.message : "Import failed."));
  }, [arxivId, router]);

  return (
    <div className="center-state">
      <div className="loader-mark" aria-hidden="true">P</div>
      {error ? (
        <>
          <p className="eyebrow">Import failed</p>
          <h1>无法打开 arXiv:{arxivId}</h1>
          <p className="state-copy" role="alert">{error}</p>
          <Link className="primary-link" href="/library">Return to library</Link>
        </>
      ) : (
        <>
          <p className="eyebrow">Resolving paper</p>
          <h1>正在检查 arXiv:{arxivId}</h1>
          <p className="state-copy">若论文尚未下载，后台任务会自动开始；已有论文将直接打开。</p>
        </>
      )}
    </div>
  );
}

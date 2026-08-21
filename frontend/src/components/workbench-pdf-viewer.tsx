"use client";

import { useEffect, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import { downloadContract } from "@/components/workbench-api";
import { errorMessage } from "@/components/workbench-utils";
import styles from "./workbench.module.css";

pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  "pdfjs-dist/build/pdf.worker.min.mjs",
  import.meta.url,
).toString();

interface WorkbenchPdfViewerProps {
  documentId: string | null;
  documentName: string | null;
  requestedPage: number;
  selectedQuote: string | null;
  onPageChange: (page: number) => void;
}

export default function WorkbenchPdfViewer({
  documentId,
  documentName,
  requestedPage,
  selectedQuote,
  onPageChange,
}: WorkbenchPdfViewerProps) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const [loadedFile, setLoadedFile] = useState<{
    documentId: string;
    url: string | null;
    error: string | null;
  } | null>(null);
  const [loadedPageCount, setLoadedPageCount] = useState<{
    documentId: string;
    count: number;
  } | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [renderWidth, setRenderWidth] = useState(680);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const updateWidth = () => setRenderWidth(Math.max(280, Math.min(920, viewport.clientWidth - 32)));
    updateWidth();
    const observer = new ResizeObserver(updateWidth);
    observer.observe(viewport);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    let active = true;
    let objectUrl: string | null = null;
    if (!documentId) return () => undefined;
    downloadContract(documentId)
      .then((blob) => {
        if (!active) return;
        objectUrl = URL.createObjectURL(blob);
        setLoadedFile({ documentId, url: objectUrl, error: null });
      })
      .catch((loadError: unknown) => {
        if (active) setLoadedFile({ documentId, url: null, error: errorMessage(loadError) });
      });
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [documentId, loadAttempt]);

  const fileUrl = loadedFile?.documentId === documentId ? loadedFile.url : null;
  const error = loadedFile?.documentId === documentId ? loadedFile.error : null;
  const loading = Boolean(documentId && loadedFile?.documentId !== documentId);
  const pageCount = loadedPageCount?.documentId === documentId ? loadedPageCount.count : 0;
  const page = pageCount > 0 ? Math.min(Math.max(requestedPage, 1), pageCount) : 1;

  if (!documentId) {
    return (
      <div className={styles.emptyDocument} ref={viewportRef}>
        <span className={styles.emptyIllustration} aria-hidden="true">
          PDF
        </span>
        <strong>No contract selected</strong>
        <p>Upload a safe PDF or select an existing document to inspect its source evidence.</p>
      </div>
    );
  }

  return (
    <section className={styles.pdfPanel} aria-label="Contract document viewer">
      <div className={styles.pdfToolbar}>
        <div className={styles.pdfIdentity}>
          <strong title={documentName ?? undefined}>{documentName ?? "Contract document"}</strong>
          <span>Authenticated source file</span>
        </div>
        <div className={styles.pageControls} aria-label="PDF page navigation">
          <button
            type="button"
            className="button secondary small"
            onClick={() => onPageChange(Math.max(1, page - 1))}
            disabled={page <= 1}
            aria-label="Previous page"
          >
            Previous
          </button>
          <label>
            <span className="sr-only">Current page</span>
            <input
              className={styles.pageInput}
              type="number"
              min={1}
              max={pageCount || 1}
              value={page}
              onChange={(event) => onPageChange(Number(event.target.value) || 1)}
            />
            <span>of {pageCount || "—"}</span>
          </label>
          <button
            type="button"
            className="button secondary small"
            onClick={() => onPageChange(Math.min(pageCount, page + 1))}
            disabled={pageCount === 0 || page >= pageCount}
            aria-label="Next page"
          >
            Next
          </button>
        </div>
      </div>
      {selectedQuote ? (
        <div className={styles.syncedEvidence} aria-live="polite">
          <span>Evidence synchronized to page {page}</span>
          <q>{selectedQuote}</q>
        </div>
      ) : null}
      <div className={styles.pdfViewport} ref={viewportRef}>
        {loading ? <div className={styles.documentLoading}>Loading protected PDF…</div> : null}
        {error ? (
          <div className="alert danger" role="alert">
            <strong>Document unavailable.</strong> {error}{" "}
            <button
              type="button"
              className="button secondary small"
              onClick={() => {
                setLoadedFile(null);
                setLoadAttempt((attempt) => attempt + 1);
              }}
            >
              Retry protected PDF
            </button>
          </div>
        ) : null}
        {fileUrl ? (
          <Document
            file={fileUrl}
            loading={<div className={styles.documentLoading}>Opening PDF…</div>}
            error={
              <div className="alert danger" role="alert">
                The PDF could not be rendered. The original file remains unchanged.
              </div>
            }
            onLoadSuccess={({ numPages }) => {
              setLoadedPageCount({ documentId, count: numPages });
              if (requestedPage > numPages) onPageChange(numPages);
            }}
          >
            <Page
              pageNumber={page}
              width={renderWidth}
              renderAnnotationLayer={false}
              renderTextLayer
              loading={<div className={styles.documentLoading}>Rendering page {page}…</div>}
            />
          </Document>
        ) : null}
      </div>
    </section>
  );
}

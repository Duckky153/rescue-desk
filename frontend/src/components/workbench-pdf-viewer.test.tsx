import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import WorkbenchPdfViewer from "@/components/workbench-pdf-viewer";
import { downloadContract } from "@/components/workbench-api";

vi.mock("@/components/workbench-api", () => ({ downloadContract: vi.fn() }));
vi.mock("react-pdf", () => ({
  pdfjs: { GlobalWorkerOptions: { workerSrc: "" } },
  Document: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Page: ({ renderAnnotationLayer }: { renderAnnotationLayer?: boolean }) => (
    <div
      data-testid="rendered-pdf-page"
      data-render-annotation-layer={String(renderAnnotationLayer)}
    />
  ),
}));

class ResizeObserverStub {
  observe() {}
  disconnect() {}
}

describe("WorkbenchPdfViewer", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal("ResizeObserver", ResizeObserverStub);
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: vi.fn(() => "blob:contract"),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: vi.fn(),
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("does not render untrusted PDF annotation actions", async () => {
    vi.mocked(downloadContract).mockResolvedValue(new Blob(["contract"]));

    render(
      <WorkbenchPdfViewer
        documentId="document-1"
        documentName="Contract.pdf"
        requestedPage={1}
        selectedQuote={null}
        onPageChange={vi.fn()}
      />,
    );

    expect(await screen.findByTestId("rendered-pdf-page")).toHaveAttribute(
      "data-render-annotation-layer",
      "false",
    );
  });

  it("retries a transient protected-file failure for the same document", async () => {
    vi.mocked(downloadContract)
      .mockRejectedValueOnce(new Error("Temporary file service failure"))
      .mockResolvedValueOnce(new Blob(["contract"]));

    render(
      <WorkbenchPdfViewer
        documentId="document-1"
        documentName="Contract.pdf"
        requestedPage={1}
        selectedQuote={null}
        onPageChange={vi.fn()}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Retry protected PDF" }));

    await waitFor(() => expect(downloadContract).toHaveBeenCalledTimes(2));
    expect(await screen.findByTestId("rendered-pdf-page")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Retry protected PDF" })).not.toBeInTheDocument();
  });
});

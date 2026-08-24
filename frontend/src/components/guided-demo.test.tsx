import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GuidedDemo } from "@/components/guided-demo";

describe("GuidedDemo", () => {
  afterEach(cleanup);

  it("explains the product, supports keyboard progression, direct section jumps, and dismissal", () => {
    const onSelectTab = vi.fn();
    const onReviewDecision = vi.fn();
    render(
      <GuidedDemo
        status="evidence_review"
        blockerCount={1}
        activeTab="evidence"
        onSelectTab={onSelectTab}
        onReviewDecision={onReviewDecision}
      />,
    );

    expect(screen.getByText("Problem")).toBeVisible();
    expect(screen.getByText("User")).toBeVisible();
    expect(screen.getByText("Outcome")).toBeVisible();
    expect(screen.getByText(/one source decision before internal readiness/i)).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: /review final decision/i }));
    expect(onReviewDecision).toHaveBeenCalledOnce();

    const start = screen.getByRole("button", { name: /start full tour/i });
    fireEvent.click(start);
    const guide = screen.getByRole("region", { name: /contract exit should not depend/i });
    expect(guide).toHaveFocus();
    expect(onSelectTab).toHaveBeenLastCalledWith("evidence");

    fireEvent.keyDown(guide, { key: "ArrowRight" });
    expect(screen.getByRole("heading", { name: /check the exact words/i })).toBeVisible();
    expect(screen.getByText(/only unfinished decision/i)).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Economics" }));
    expect(onSelectTab).toHaveBeenLastCalledWith("economics");
    expect(screen.getByRole("heading", { name: /deterministic code does the math/i })).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Restart" }));
    expect(screen.getByRole("heading", { name: /contract exit should not depend/i })).toBeVisible();

    fireEvent.keyDown(screen.getByRole("region", { name: /contract exit should not depend/i }), {
      key: "Escape",
    });
    expect(screen.queryByText("Three-minute product tour")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /start full tour/i })).toHaveFocus();
  });

  it("describes an exported case as the inspectable completed outcome", () => {
    render(
      <GuidedDemo
        status="exported"
        blockerCount={0}
        isSeededCompletedExample
        activeTab="readiness"
        onSelectTab={vi.fn()}
        onReviewDecision={vi.fn()}
      />,
    );
    expect(screen.getByText(/role-separated approver history and four exports/i)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /start full tour/i }));
    for (let index = 0; index < 5; index += 1) {
      fireEvent.click(screen.getByRole("button", { name: "Next" }));
    }
    expect(screen.getByRole("heading", { name: /seeded example records an exported/i })).toBeVisible();
    expect(screen.getByText(/not represented as a human approval/i)).toBeVisible();
    expect(screen.getByRole("link", { name: /compare both matters/i })).toHaveAttribute("href", "/");
  });
});

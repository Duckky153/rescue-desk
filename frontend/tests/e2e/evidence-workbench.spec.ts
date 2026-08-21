import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { createHash } from "node:crypto";
import { readFile, stat } from "node:fs/promises";
import { resolve } from "node:path";

const ANALYST_EMAIL = "analyst@rescuedesk.local";
const APPROVER_EMAIL = "approver@rescuedesk.local";
const ANALYST_PASSWORD = "DemoPassword!2026";
const GUIDED_MATTER = "Northstar guided review - 1 fact left";
const COMPLETED_MATTER = "Northstar completed exit packet";
const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

async function enforceReadOnlyApi(page: Page): Promise<string[]> {
  const blockedWrites: string[] = [];

  await page.route("http://127.0.0.1:8000/**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    const isAuthentication =
      request.method() === "POST" && pathname === "/v1/auth/token";

    if (MUTATING_METHODS.has(request.method()) && !isAuthentication) {
      blockedWrites.push(`${request.method()} ${pathname}`);
      await route.abort("blockedbyclient");
      return;
    }

    await route.continue();
  });

  return blockedWrites;
}

async function signIn(page: Page, email = ANALYST_EMAIL): Promise<void> {
  await page.goto("/login");
  await expect(page.getByRole("heading", { name: "Sign in to review matters" })).toBeVisible();
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(ANALYST_PASSWORD);
  await page.getByRole("button", { name: "Open evidence workspace" }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("heading", { name: "Exit-review matters" })).toBeVisible();
}

async function openMatter(page: Page, matterName: string): Promise<void> {
  await page.getByRole("link", { name: new RegExp(matterName) }).first().click();
  await expect(page).toHaveURL(/\/cases\/[0-9a-f-]+$/);
  await expect(page.getByRole("heading", { name: matterName })).toBeVisible();
}

async function expectCaseStatus(page: Page, status: string): Promise<void> {
  await expect(page.locator("main header").locator(".status-pill")).toHaveText(status);
}

async function reviewFact(
  page: Page,
  label: string,
  decision: "accept" | "correct" | "reject",
  correction?: { normalized: Record<string, unknown> },
): Promise<void> {
  const rail = page.getByRole("complementary", { name: "Extracted assertions" });
  const card = rail.getByText(label, { exact: true }).first().locator("xpath=ancestor::button[1]");
  await card.click();
  const decisions = rail.getByRole("group", { name: "Review decision" });
  await decisions.getByRole("button", { name: new RegExp(`^${decision}$`, "i") }).click();
  if (decision === "correct") {
    if (!correction) throw new Error(`Correction data is required for ${label}`);
    await rail.getByLabel("Normalized value (JSON)").fill(JSON.stringify(correction.normalized));
    await rail.getByRole("checkbox", { name: /explicit assumption/i }).check();
  }
  await rail
    .getByLabel("Reason for this decision")
    .fill(`Browser lifecycle ${decision} decision verified against the displayed synthetic source.`);
  await rail.getByRole("button", { name: new RegExp(`^Record ${decision}$`, "i") }).click();
  const expectedState = decision === "accept" ? "accepted" : decision === "correct" ? "corrected" : "rejected";
  const refreshedCard = rail
    .getByText(label, { exact: true })
    .first()
    .locator("xpath=ancestor::button[1]");
  await expect(refreshedCard).toContainText(new RegExp(expectedState, "i"));
}

async function openSeededMatter(page: Page): Promise<void> {
  await signIn(page);
  const demoRoute = page.getByRole("region", {
    name: "Make one live decision against synthetic evidence, then inspect the finished result.",
  });
  await demoRoute.getByRole("link", { name: "Start guided case" }).click();
  await expect(page).toHaveURL(/\/cases\/[0-9a-f-]+$/);
  await expect(page.getByRole("heading", { name: GUIDED_MATTER })).toBeVisible();
}

async function expectSingleMainWithoutPageOverflow(page: Page): Promise<void> {
  await expect(page.locator("main")).toHaveCount(1);
  const dimensions = await page.evaluate(() => ({
    bodyScrollWidth: document.body.scrollWidth,
    documentScrollWidth: document.documentElement.scrollWidth,
    viewportWidth: window.innerWidth,
  }));

  expect(
    dimensions.documentScrollWidth,
    `document overflowed viewport: ${JSON.stringify(dimensions)}`,
  ).toBeLessThanOrEqual(dimensions.viewportWidth + 1);
  expect(
    dimensions.bodyScrollWidth,
    `body overflowed viewport: ${JSON.stringify(dimensions)}`,
  ).toBeLessThanOrEqual(dimensions.viewportWidth + 1);
}

async function expectNoSeriousAccessibilityViolations(page: Page): Promise<void> {
  const analysis = await new AxeBuilder({ page }).analyze();
  const severe = analysis.violations.filter(
    (violation) => violation.impact === "serious" || violation.impact === "critical",
  );
  const report = severe
    .map(
      (violation) =>
        `${violation.id} (${violation.impact}): ${violation.help}\n${violation.nodes
          .map((node) => `  ${node.target.join(" ")} — ${node.failureSummary ?? ""}`)
          .join("\n")}`,
    )
    .join("\n\n");

  expect(severe, report || "No serious or critical axe violations").toEqual([]);
}

test("local analyst can sign in and discover the two-step demo route without changing it", async ({
  page,
}) => {
  const blockedWrites = await enforceReadOnlyApi(page);

  await page.goto("/login");
  await expect(page.getByRole("heading", { name: "Turn a difficult contract into an inspectable decision." })).toBeVisible();
  await expect(page.getByText("The local demonstration account is prefilled.")).toBeVisible();
  await expectSingleMainWithoutPageOverflow(page);
  await expectNoSeriousAccessibilityViolations(page);

  await signIn(page);
  await expect(page.getByText(/\d+ total/)).toBeVisible();
  const demoRoute = page.getByRole("region", {
    name: "Make one live decision against synthetic evidence, then inspect the finished result.",
  });
  await expect(demoRoute.getByText("Synthetic data only · no external action")).toBeVisible();
  await expect(demoRoute.getByRole("link", { name: "Start guided case" })).toBeVisible();
  await expect(demoRoute.getByRole("link", { name: "Open completed example" })).toBeVisible();

  const guidedMatter = page.getByRole("link", { name: new RegExp(GUIDED_MATTER) }).last();
  const guidedRow = guidedMatter.locator("xpath=ancestor::tr[1]");
  await expect(guidedMatter).toContainText("Northstar Systems");
  await expect(guidedRow.getByRole("cell", { name: "LegacySuite ERP" })).toBeVisible();
  await expect(guidedRow.getByText("Evidence review", { exact: true })).toBeVisible();
  const completedRow = page
    .getByRole("link", { name: new RegExp(COMPLETED_MATTER) })
    .last()
    .locator("xpath=ancestor::tr[1]");
  await expect(completedRow.getByText("Exported", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  const matterList = page.getByRole("region", { name: "Matter list" });
  await matterList.focus();
  await expect(matterList).toBeFocused();
  await page.keyboard.press("ArrowRight");
  await expectSingleMainWithoutPageOverflow(page);
  await expectNoSeriousAccessibilityViolations(page);
  expect(blockedWrites).toEqual([]);
});

test("guided tour teaches the workflow and navigates every section with keyboard support", async ({
  page,
}) => {
  const blockedWrites = await enforceReadOnlyApi(page);
  await openSeededMatter(page);

  await expect(page.getByRole("heading", { name: "Understand the product before clicking through it." })).toBeVisible();
  await expect(page.getByText(/one source decision before internal readiness/i)).toBeVisible();
  await expect(page.getByText("Problem", { exact: true })).toBeVisible();
  await expect(page.getByText("User", { exact: true })).toBeVisible();
  await expect(page.getByText("Outcome", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Start guided demo" }).click();

  let guide = page.getByRole("region", { name: /contract exit should not depend/i });
  await expect(guide).toBeFocused();
  await guide.press("ArrowRight");
  guide = page.getByRole("region", { name: /check the exact words/i });
  await expect(guide).toContainText("Renewal · notice days");
  const rail = page.getByRole("complementary", { name: "Extracted assertions" });
  await expect(rail.getByText("One evidence decision left")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Renewal · notice days" })).toBeVisible();
  await expect(rail.getByRole("button", { name: "Record accept" })).toBeVisible();

  await guide.getByRole("button", { name: "Economics" }).click();
  guide = page.getByRole("region", { name: /deterministic code does the math/i });
  await expect(page.getByRole("heading", { name: "Switching scenario" })).toBeVisible();
  await guide.press("ArrowRight");
  guide = page.getByRole("region", { name: /uncertainty blocks the workflow/i });
  await expect(page.getByRole("heading", { name: "Internal review readiness" })).toBeVisible();
  await guide.getByRole("button", { name: "Audit" }).click();
  guide = page.getByRole("region", { name: /consequential action leaves/i });
  await expect(page.getByRole("heading", { name: "Audit ledger" })).toBeVisible();

  const restart = guide.getByRole("button", { name: "Restart" });
  await restart.hover();
  await expect(restart).toHaveCSS("color", "rgb(255, 255, 255)");

  await guide.press("Escape");
  await expect(page.getByRole("button", { name: "Start guided demo" })).toBeFocused();
  await expectSingleMainWithoutPageOverflow(page);
  await expectNoSeriousAccessibilityViolations(page);
  expect(blockedWrites).toEqual([]);
});

test("seeded workbench connects the real PDF citation to deterministic economics", async ({
  page,
}) => {
  const blockedWrites = await enforceReadOnlyApi(page);
  await openSeededMatter(page);

  const documents = page.getByRole("complementary", { name: "Case documents" });
  await expect(documents.getByText("synthetic-northstar-contract.pdf")).toBeVisible();
  await expect(documents.getByText("Source classification: Synthetic")).toBeVisible();
  await expect(documents.getByText("Passed", { exact: true })).toBeVisible();
  await expect(documents.getByText("Processed", { exact: true })).toBeVisible();

  const viewer = page.getByRole("region", { name: "Contract document viewer" });
  await expect(viewer.getByText("Authenticated source file")).toBeVisible();
  await expect(viewer.locator("canvas.react-pdf__Page__canvas")).toBeVisible({ timeout: 30_000 });
  await expect(viewer.getByLabel("Current page")).toHaveValue("1");

  const assertionRail = page.getByRole("complementary", { name: "Extracted assertions" });
  await assertionRail
    .getByRole("button", { name: /Fee · subscription.*USD 120,000\.00/i })
    .click();
  await expect(page.getByRole("heading", { name: "Fee · subscription" })).toBeVisible();
  const citation = page.getByRole("button", {
    name: /Page 1.*Annual Subscription Fee: USD \$120,000\.00, billed annually\./,
  });
  await expect(citation).toBeVisible();
  await citation.click();
  await expect(viewer.getByText("Evidence synchronized to page 1")).toBeVisible();
  await expect(
    viewer.locator("q").filter({
      hasText: "Annual Subscription Fee: USD $120,000.00, billed annually.",
    }),
  ).toBeVisible();
  await expect(page.getByText(/quote sha256 a35f496db5937c…/)).toBeVisible();
  await expectSingleMainWithoutPageOverflow(page);
  await expectNoSeriousAccessibilityViolations(page);

  await page.getByRole("button", { name: /^Economics/ }).click();
  const obligationLedger = page
    .getByRole("heading", { name: "Obligation ledger" })
    .locator("xpath=ancestor::section[1]");
  await expect(obligationLedger).toContainText("Active input total · USD");
  await expect(obligationLedger).toContainText("$120,000.00");
  await expect(obligationLedger).toContainText("Jan 15, 2026 – Jan 15, 2027");
  await expect(obligationLedger).toContainText("Money evidence reviewed");

  const scenario = page
    .getByRole("heading", { name: "Switching scenario" })
    .locator("xpath=ancestor::section[1]");
  await expect(scenario).toContainText("Reproducible");
  await expect(scenario).toContainText("As of Aug 20, 2026");
  await expect(scenario).toContainText("Engine remaining-subscription-v2");
  await expect(scenario).toContainText("USD documented remainder");
  await expect(scenario).toContainText("$48,657.53");
  await expect(scenario).toContainText("$0.00 – $48,657.53");
  await expect(scenario).toContainText("contract-daily-half-open-v1");
  await expect(scenario).toContainText("No deterministic calculation blockers remain.");
  await expectSingleMainWithoutPageOverflow(page);
  await expectNoSeriousAccessibilityViolations(page);
  expect(blockedWrites).toEqual([]);
});

test("protected PDF loading state remains accessible on the mobile workbench", async (
  { page },
  testInfo,
) => {
  test.skip(
    testInfo.project.name !== "mobile-chromium",
    "The transient loading-state regression targets the constrained mobile viewport.",
  );
  const blockedWrites = await enforceReadOnlyApi(page);
  await page.route("http://127.0.0.1:8000/v1/documents/*/file", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1_000));
    await route.fallback();
  });

  await openSeededMatter(page);
  const viewer = page.getByRole("region", { name: "Contract document viewer" });
  await expect(viewer.getByText("Loading protected PDF…")).toBeVisible();
  await expectNoSeriousAccessibilityViolations(page);
  expect(blockedWrites).toEqual([]);
});

test("transient protected-PDF and page-catalog failures can retry without changing sources", async (
  { page },
  testInfo,
) => {
  test.skip(
    testInfo.project.name !== "desktop-chromium",
    "Run the same-document source-load recovery proof once on desktop.",
  );
  const blockedWrites = await enforceReadOnlyApi(page);
  let failedFile = false;
  let failedPages = false;
  let fileRequests = 0;
  let pageRequests = 0;
  await page.route("http://127.0.0.1:8000/v1/documents/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.endsWith("/file")) {
      fileRequests += 1;
      if (!failedFile) {
        failedFile = true;
        await route.abort("failed");
        return;
      }
    }
    if (pathname.endsWith("/pages")) {
      pageRequests += 1;
      if (!failedPages) {
        failedPages = true;
        await route.abort("failed");
        return;
      }
    }
    await route.fallback();
  });

  await openSeededMatter(page);
  await page.getByRole("button", { name: "Retry page catalog" }).click();
  await expect(page.getByRole("button", { name: "Retry page catalog" })).not.toBeVisible();
  await page.getByRole("button", { name: "Retry protected PDF" }).click();
  await expect(page.locator("canvas.react-pdf__Page__canvas")).toBeVisible({ timeout: 30_000 });
  expect(pageRequests).toBe(2);
  expect(fileRequests).toBe(2);
  expect(blockedWrites).toEqual([]);
});

test("one-decision readiness and the completed approved packet remain inspectable", async ({ page }) => {
  const blockedWrites = await enforceReadOnlyApi(page);
  await openSeededMatter(page);

  await page.getByRole("button", { name: /^Readiness & packet/ }).click();
  const readiness = page
    .getByRole("heading", { name: "Internal review readiness" })
    .locator("xpath=ancestor::section[1]");
  await expect(readiness).toContainText("1 blocker");
  await expect(readiness).toContainText("Mandatory facts");
  await expect(readiness).toContainText("Reviewed against source evidence");
  await expect(readiness).toContainText("Calculation");
  await expect(readiness).toContainText("Versioned result is available");
  await expect(readiness).toContainText("1 must be resolved");
  await expect(readiness.getByText("Assertion requires review")).toHaveCount(1);
  await expect(page.getByRole("button", { name: "Send to internal review" })).toBeDisabled();

  const packet = page
    .getByRole("heading", { name: "Review packet" })
    .locator("xpath=ancestor::section[1]");
  await expect(packet).toContainText("Approved packet formats remain locked until an approver records internal approval.");
  await expect(packet).toContainText("Internal review packet");
  await expect(packet).toContainText("Switching evidence brief");
  await expect(packet).toContainText("Evidence ledger");
  await expect(packet).toContainText("Canonical snapshot");
  await expect(packet.getByText("No current artifact generated")).toHaveCount(4);
  await expect(packet.getByRole("button", { name: "Download" })).toHaveCount(0);
  await expect(packet.getByRole("button", { name: "Generate" })).toHaveCount(4);
  for (const button of await packet.getByRole("button", { name: "Generate" }).all()) {
    await expect(button).toBeDisabled();
  }
  await expectSingleMainWithoutPageOverflow(page);
  await expectNoSeriousAccessibilityViolations(page);

  await page.getByRole("button", { name: /^Audit/ }).click();
  const audit = page
    .getByRole("heading", { name: "Audit ledger" })
    .locator("xpath=ancestor::section[1]");
  await expect(audit).toContainText(/\d+ events/);
  await expect(audit).toContainText(/\d+ actions/);
  await expect(audit).toContainText(/\d+ actors/);
  await expect(audit).toContainText("Case · created");
  await expect(audit).toContainText("Previous hash recorded");
  await expect(audit).toContainText("does not independently verify them");
  const caseCreatedSummary = audit
    .getByText("Case · created")
    .locator("xpath=ancestor::summary[1]");
  await caseCreatedSummary.click();
  const eventDetails = caseCreatedSummary.locator("xpath=following-sibling::div[1]");
  await expect(eventDetails).toContainText("demo-guided-case");
  await expect(eventDetails.getByText(/^[a-f0-9]{64}$/)).toHaveCount(2);
  await expect(eventDetails).toContainText(`"display_name": "${GUIDED_MATTER}"`);
  await expectSingleMainWithoutPageOverflow(page);
  await expectNoSeriousAccessibilityViolations(page);

  await page.locator("main header").getByRole("link", { name: "Matters", exact: true }).click();
  await page
    .getByRole("region", {
      name: "Make one live decision against synthetic evidence, then inspect the finished result.",
    })
    .getByRole("link", { name: "Open completed example" })
    .click();
  await expect(page.getByRole("heading", { name: COMPLETED_MATTER })).toBeVisible();
  await expectCaseStatus(page, "Exported");
  await page.getByRole("button", { name: /^Readiness & packet/ }).click();
  const completedReadiness = page
    .getByRole("heading", { name: "Internal review readiness" })
    .locator("xpath=ancestor::section[1]");
  await expect(completedReadiness).toContainText("Ready");
  await expect(completedReadiness).toContainText("No blocking findings");
  const completedPacket = page
    .getByRole("heading", { name: "Review packet" })
    .locator("xpath=ancestor::section[1]");
  await expect(completedPacket).toContainText("4 current · 0 historical");
  await expect(
    completedPacket.getByText("Seeded synthetic approver-role snapshot"),
  ).toHaveCount(4);
  await expect(completedPacket.getByRole("button", { name: "Download" })).toHaveCount(4);
  await expect(completedPacket.getByText(/packet [a-f0-9]{12}…/)).toHaveCount(4);
  await expectSingleMainWithoutPageOverflow(page);
  await expectNoSeriousAccessibilityViolations(page);
  expect(blockedWrites).toEqual([]);
});

test("analyst can add a synthetic obligation and see it persist without changing the seeded matter", async (
  { page },
  testInfo,
) => {
  test.skip(
    testInfo.project.name !== "desktop-chromium",
    "Run the stateful proof once; the read-only workbench coverage still runs at both viewports.",
  );

  const suffix = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
  const matterName = `Browser mutation proof ${suffix}`;
  await signIn(page);

  await page.getByRole("button", { name: "New matter" }).click();
  await page.getByLabel("Matter name").fill(matterName);
  await page.getByLabel("Company").fill(`Synthetic Company ${suffix}`);
  await page.getByLabel("Legacy ERP").fill("Synthetic ERP");
  await page.getByRole("button", { name: "Open matter" }).click();

  const createdMatter = page.getByRole("link", { name: new RegExp(matterName) }).first();
  await expect(createdMatter).toBeVisible();
  await createdMatter.click();
  await expect(page.getByRole("heading", { name: matterName })).toBeVisible();

  await page.getByRole("button", { name: /^Economics/ }).click();
  await page.getByLabel("Active input amount").fill("1,234.56");
  await page.getByLabel("Service start").fill("2026-01-01");
  await page.getByLabel("Service end").fill("2026-12-31");
  await page.getByLabel("Payment status").selectOption("unpaid");
  await page.getByRole("button", { name: "Add obligation" }).click();

  const ledger = page
    .getByRole("heading", { name: "Obligation ledger" })
    .locator("xpath=ancestor::section[1]");
  await expect(ledger).toContainText("$1,234.56");
  await expect(ledger).toContainText("Money evidence not reviewed");

  await page.getByLabel("Calculate remaining obligations as of").fill("2026-08-21");
  await page.getByRole("button", { name: "Run calculation" }).click();
  const scenario = page
    .getByRole("heading", { name: "Switching scenario" })
    .locator("xpath=ancestor::section[1]");
  await expect(scenario).toContainText("Reproducible");
  await expect(scenario).toContainText("As of Aug 21, 2026");
  await expect(scenario).toContainText("fee has not been reviewed");

  await page.reload();
  await page.getByRole("button", { name: /^Economics/ }).click();
  await expect(ledger).toContainText("$1,234.56");
  await expect(ledger).toContainText("Jan 1, 2026 – Dec 31, 2026");
  await expect(scenario).toContainText("Reproducible");

  await page.getByRole("button", { name: /^Readiness & packet/ }).click();
  const readiness = page
    .getByRole("heading", { name: "Internal review readiness" })
    .locator("xpath=ancestor::section[1]");
  await expect(readiness).toContainText("blocker");
  await expect(readiness).toContainText("Versioned result is available");

  await page.getByRole("button", { name: /^Audit/ }).click();
  const audit = page
    .getByRole("heading", { name: "Audit ledger" })
    .locator("xpath=ancestor::section[1]");
  await expect(audit).toContainText("Fee · created");
  await expect(audit).toContainText("Previous hash recorded");
  await expectNoSeriousAccessibilityViolations(page);
});

test("a saved mutation with a failed snapshot refresh locks retry until a fresh reload", async (
  { page },
  testInfo,
) => {
  test.skip(
    testInfo.project.name !== "desktop-chromium",
    "Run the response-loss mutation proof once at the desktop workflow viewport.",
  );

  const suffix = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
  const matterName = `Refresh recovery proof ${suffix}`;
  await signIn(page);
  await page.getByRole("button", { name: "New matter" }).click();
  await page.getByLabel("Matter name").fill(matterName);
  await page.getByLabel("Company").fill(`Synthetic Refresh Company ${suffix}`);
  await page.getByLabel("Legacy ERP").fill("Synthetic ERP");
  await page.getByRole("button", { name: "Open matter" }).click();
  await openMatter(page, matterName);

  const caseId = new URL(page.url()).pathname.split("/").at(-1)!;
  let feePosts = 0;
  let failNextCaseSnapshot = false;
  await page.route("http://127.0.0.1:8000/**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (request.method() === "POST" && pathname === `/v1/cases/${caseId}/fees`) {
      feePosts += 1;
      const response = await route.fetch();
      failNextCaseSnapshot = true;
      await route.fulfill({ response });
      return;
    }
    if (
      failNextCaseSnapshot &&
      request.method() === "GET" &&
      pathname === `/v1/cases/${caseId}/workbench-snapshot`
    ) {
      failNextCaseSnapshot = false;
      await route.abort("failed");
      return;
    }
    await route.fallback();
  });

  await page.getByRole("button", { name: /^Economics/ }).click();
  await page.getByLabel("Active input amount").fill("9.99");
  await page.getByRole("button", { name: "Add obligation" }).click();

  await expect(page.getByText(/change was saved.*could not refresh/i)).toBeVisible();
  await expect(page.getByText(/obligation was saved.*could not refresh/i)).toBeVisible();
  await expect(page.getByRole("button", { name: "Add obligation" })).toBeDisabled();
  expect(feePosts).toBe(1);

  await page.getByRole("button", { name: "Reload fresh snapshot" }).click();
  await expect(page.getByRole("button", { name: "Add obligation" })).toBeEnabled();
  await expect(page.getByRole("region", { name: "Obligation ledger" })).toContainText("$9.99");
  expect(feePosts).toBe(1);
});

test("a created matter remains visible and creation locks when list refresh fails", async (
  { page },
  testInfo,
) => {
  test.skip(
    testInfo.project.name !== "desktop-chromium",
    "Run the committed-create/list-refresh recovery proof once on desktop.",
  );

  const suffix = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
  const matterName = `Matter list recovery proof ${suffix}`;
  await signIn(page);

  let createPosts = 0;
  let failNextList = false;
  await page.route("http://127.0.0.1:8000/v1/cases", async (route) => {
    const request = route.request();
    if (request.method() === "POST") {
      createPosts += 1;
      const response = await route.fetch();
      failNextList = true;
      await route.fulfill({ response });
      return;
    }
    if (request.method() === "GET" && failNextList) {
      failNextList = false;
      await route.abort("failed");
      return;
    }
    await route.fallback();
  });

  await page.getByRole("button", { name: "New matter" }).click();
  await page.getByLabel("Matter name").fill(matterName);
  await page.getByLabel("Company").fill(`Synthetic List Company ${suffix}`);
  await page.getByLabel("Legacy ERP").fill("Synthetic ERP");
  await page.getByRole("button", { name: "Open matter" }).click();

  await expect(page.getByText(/matter was created and is shown from its save receipt/i)).toBeVisible();
  await expect(page.getByRole("link", { name: new RegExp(matterName) })).toHaveCount(1);
  await expect(page.getByRole("button", { name: "New matter" })).toBeDisabled();
  expect(createPosts).toBe(1);

  await page.getByRole("button", { name: "Reload matters" }).click();
  await expect(page.getByRole("button", { name: "New matter" })).toBeEnabled();
  await expect(page.getByRole("link", { name: new RegExp(matterName) })).toHaveCount(1);
  expect(createPosts).toBe(1);
});

test("superseding a live source makes its sole assertion ineligible for acceptance", async (
  { page },
  testInfo,
) => {
  test.skip(
    testInfo.project.name !== "desktop-chromium",
    "Run the stateful source-governance proof once on desktop.",
  );
  const suffix = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
  const matterName = `Source supersession proof ${suffix}`;
  await signIn(page);
  await page.getByRole("button", { name: "New matter" }).click();
  await page.getByLabel("Matter name").fill(matterName);
  await page.getByLabel("Company").fill(`Synthetic Source Company ${suffix}`);
  await page.getByLabel("Legacy ERP").fill("Synthetic ERP");
  await page.getByRole("button", { name: "Open matter" }).click();
  await openMatter(page, matterName);

  const fixtureRoot = resolve(process.cwd(), "../fixtures/contracts");
  const documents = page.getByRole("complementary", { name: "Case documents" });
  const fileInput = page.getByLabel("Contract PDF file");
  await documents.getByLabel("Document source").selectOption("synthetic");
  await fileInput.setInputFiles(resolve(fixtureRoot, "clean_standard.pdf"));
  await expect(documents.getByText("clean_standard.pdf")).toBeVisible({ timeout: 30_000 });
  await documents.getByLabel("Document source").selectOption("redacted");
  await fileInput.setInputFiles(resolve(fixtureRoot, "redacted_terms.pdf"));
  await expect(documents.getByText("redacted_terms.pdf")).toBeVisible({ timeout: 30_000 });

  const cleanSource = documents.getByRole("button", { name: /clean_standard\.pdf/i });
  await cleanSource.click();
  await page.getByRole("button", { name: /Fee · subscription/ }).click();
  await expect(page.getByRole("button", { name: "Accept", exact: true })).toBeVisible();

  await documents
    .getByLabel("Replacement reason")
    .fill("The redacted synthetic source replaces this source for the recovery proof.");
  await documents.getByRole("button", { name: "Supersede selected source" }).click();
  await expect(cleanSource).toContainText("Superseded");

  const inactiveFee = page.getByRole("button", {
    name: /Fee · subscription.*Superseded source/i,
  });
  await expect(inactiveFee).toBeVisible();
  await inactiveFee.click();
  await expect(page.getByText(/cites only superseded documents/i)).toBeVisible();
  const decisions = page.getByRole("group", { name: "Review decision" });
  await expect(decisions.getByRole("button", { name: "Accept", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Correct" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Reject" })).toBeVisible();
  await page.getByRole("button", { name: "Correct" }).click();
  await expect(page.getByRole("checkbox", { name: /explicit assumption/i })).toBeChecked();

  await page.getByRole("button", { name: /^Economics/ }).click();
  const evidenceSelect = page.getByLabel("Supporting reviewed fact");
  await expect(
    evidenceSelect.locator("option").filter({ hasText: "Fee · subscription" }),
  ).toHaveCount(0);
});

test("complete synthetic matter lifecycle remains inspectable through approval, export, and reopen", async (
  { page },
  testInfo,
) => {
  test.skip(
    testInfo.project.name !== "desktop-chromium",
    "Run the long stateful lifecycle once; read-only coverage remains dual-viewport.",
  );
  test.setTimeout(120_000);
  const suffix = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
  const matterName = `Full lifecycle proof ${suffix}`;
  const createKeys: string[] = [];
  let loseFirstCreateResponse = true;

  await page.route("http://127.0.0.1:8000/v1/cases", async (route) => {
    const request = route.request();
    if (request.method() !== "POST") {
      await route.fallback();
      return;
    }
    createKeys.push(request.headers()["idempotency-key"] ?? "");
    const response = await route.fetch();
    if (loseFirstCreateResponse) {
      loseFirstCreateResponse = false;
      await route.abort("failed");
      return;
    }
    await route.fulfill({ response });
  });

  await signIn(page);
  await page.getByRole("button", { name: "New matter" }).click();
  await page.getByLabel("Matter name").fill(matterName);
  await page.getByLabel("Company").fill(`Synthetic Lifecycle Company ${suffix}`);
  await page.getByLabel("Legacy ERP").fill("Synthetic Legacy ERP");
  await page.getByRole("button", { name: "Open matter" }).click();
  await expect(page.getByText("Unable to create matter", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Open matter" }).click();

  const matterLinks = page.getByRole("link", { name: new RegExp(matterName) });
  await expect(matterLinks).toHaveCount(1);
  expect(createKeys).toHaveLength(2);
  expect(createKeys[0]).toBeTruthy();
  expect(createKeys[1]).toBe(createKeys[0]);
  await openMatter(page, matterName);

  const fixtureRoot = resolve(process.cwd(), "../fixtures/contracts");
  const documents = page.getByRole("complementary", { name: "Case documents" });
  const fileInput = page.getByLabel("Contract PDF file");
  await documents.getByLabel("Document source").selectOption("synthetic");
  await fileInput.setInputFiles(resolve(fixtureRoot, "clean_standard.pdf"));
  await expect(documents.getByText("clean_standard.pdf")).toBeVisible({ timeout: 30_000 });
  await expect(documents.getByText("Processed", { exact: true })).toHaveCount(1);
  await expect(documents.getByText("Source classification: Synthetic")).toBeVisible();

  await documents.getByLabel("Document source").selectOption("redacted");
  await fileInput.setInputFiles(resolve(fixtureRoot, "redacted_terms.pdf"));
  await expect(documents.getByText("redacted_terms.pdf")).toBeVisible({ timeout: 30_000 });
  await expect(documents.getByText("Processed", { exact: true })).toHaveCount(2);
  await expect(documents.getByText("Source classification: Redacted")).toBeVisible();
  await expect(documents.getByText(/redacted.*does not certify/i)).toBeVisible();
  await expect(page.getByText("Evidence review", { exact: true }).first()).toBeVisible();

  await reviewFact(page, "Contract start date", "accept");
  await reviewFact(page, "Contract end date", "correct", {
    normalized: { type: "date", value: "2029-01-14" },
  });
  await reviewFact(page, "Fee · subscription", "accept");
  await reviewFact(page, "Fee · termination", "reject");
  await reviewFact(page, "Auto renewal", "accept");
  await reviewFact(page, "Renewal · notice days", "correct", {
    normalized: { type: "duration_days", days: 90 },
  });
  await expect(
    page.getByRole("complementary", { name: "Extracted assertions" }).getByText("Queue clear"),
  ).toBeVisible();

  await page.getByRole("button", { name: /^Economics/ }).click();
  const ledger = page
    .getByRole("heading", { name: "Obligation ledger" })
    .locator("xpath=ancestor::section[1]");

  await page.getByLabel("Active input amount").fill("1.00");
  await page.getByLabel("Payment status").selectOption("unpaid");
  await page.getByRole("button", { name: "Add obligation" }).click();
  await expect(ledger).toContainText("$1.00");
  await ledger
    .getByRole("button", { name: /supersede subscription obligation \$1\.00/i })
    .click();
  await page
    .getByLabel("Recovery reason")
    .fill("Synthetic recovery proof: this row was entered from the wrong schedule.");
  await page.getByRole("button", { name: "Confirm supersede" }).click();
  await expect(ledger).toContainText("Superseded");
  await expect(ledger).toContainText("this row was entered from the wrong schedule");

  await page.getByLabel("Active input amount").fill("120000.00");
  await page.getByLabel("Payment status").selectOption("unpaid");
  await page.getByLabel("Billing cadence").selectOption("annual");
  await page.getByLabel("Proration rule").selectOption("contract_daily");
  await page.getByLabel("Service start").fill("2026-01-15");
  await page.getByLabel("Service end").fill("2027-01-15");
  const evidenceSelect = page.getByLabel("Supporting reviewed fact");
  const subscriptionOption = evidenceSelect.locator("option").filter({ hasText: "Fee · subscription" });
  await expect(subscriptionOption).toHaveCount(1);
  await evidenceSelect.selectOption(await subscriptionOption.getAttribute("value"));
  await page.getByLabel(/verified this obligation/i).check();
  await page.getByRole("button", { name: "Add obligation" }).click();
  await expect(ledger).toContainText("$120,000.00");
  await expect(ledger).toContainText("Money evidence reviewed");

  await page.getByLabel("Calculate remaining obligations as of").fill("2026-08-20");
  await page.getByRole("button", { name: "Run calculation" }).click();
  const scenario = page
    .getByRole("heading", { name: "Switching scenario" })
    .locator("xpath=ancestor::section[1]");
  await expect(scenario).toContainText("Reproducible");
  await expect(scenario).toContainText("$48,657.53");

  await page.getByRole("button", { name: "Sign out" }).click();
  await signIn(page, APPROVER_EMAIL);
  await openMatter(page, matterName);
  await page.getByRole("button", { name: /^Readiness & packet/ }).click();

  let dispositionButtons = page.getByRole("button", { name: "Record disposition" });
  expect(await dispositionButtons.count()).toBeGreaterThan(0);
  while ((await dispositionButtons.count()) > 0) {
    const before = await dispositionButtons.count();
    await dispositionButtons.first().click();
    await page.getByLabel("Disposition", { exact: true }).selectOption("accepted_risk");
    await page
      .getByLabel("Disposition reason")
      .fill("Synthetic redacted terms were excluded; the clean fixture supplies the reviewed evidence.");
    const dispositionForm = page
      .getByLabel("Disposition reason")
      .locator("xpath=ancestor::form[1]");
    await dispositionForm.getByRole("button", { name: "Record disposition" }).click();
    dispositionButtons = page.getByRole("button", { name: "Record disposition" });
    await expect(dispositionButtons).toHaveCount(before - 1);
  }

  const readiness = page
    .getByRole("heading", { name: "Internal review readiness" })
    .locator("xpath=ancestor::section[1]");
  await expect(readiness).toContainText("Ready");
  await expect(readiness).toContainText("No blocking findings");
  await expect(readiness).toContainText("Reviewed; explicit assumptions remain labelled");
  await page
    .getByLabel("Decision reason")
    .fill("All mandatory facts and the deterministic calculation are ready for internal review.");
  await page.getByRole("button", { name: "Send to internal review" }).click();
  await expectCaseStatus(page, "Ready for review");

  await page
    .getByLabel("Return reason")
    .fill("A synthetic pre-approval correction path must remain available to the approver.");
  await page.getByRole("button", { name: "Return to evidence review" }).click();
  await expectCaseStatus(page, "Evidence review");
  await page
    .getByLabel("Decision reason")
    .fill("The correction path was inspected; unchanged evidence remains ready for review.");
  await page.getByRole("button", { name: "Send to internal review" }).click();
  await expectCaseStatus(page, "Ready for review");

  await page
    .getByLabel("Decision reason")
    .fill("Assigned approver verified the synthetic evidence, formulas, and unresolved-risk record.");
  await page.getByRole("button", { name: "Approve internal packet" }).click();
  await expectCaseStatus(page, "Internally approved");

  const packet = page
    .getByRole("heading", { name: "Review packet" })
    .locator("xpath=ancestor::section[1]");
  for (const title of [
    "Internal review packet",
    "Switching evidence brief",
    "Evidence ledger",
    "Canonical snapshot",
  ]) {
    const article = packet.getByText(title, { exact: true }).locator("xpath=ancestor::article[1]");
    await article.getByRole("button", { name: /^(Generate|Regenerate approved)$/ }).click();
    await expect(article.getByRole("button", { name: "Download" })).toBeVisible();
    await expect(article).toContainText("Approver-recorded snapshot");
  }

  const packetHashes = await packet.getByText(/^packet [a-f0-9]{12}…$/).allTextContents();
  expect(packetHashes).toHaveLength(4);
  expect(new Set(packetHashes).size).toBe(1);

  const exportArticles = packet.locator("article");
  await expect(exportArticles).toHaveCount(4);
  const downloadedPacketHashes: string[] = [];
  for (const article of await exportArticles.all()) {
    const articleText = await article.textContent();
    const expectedFileHashPrefix = articleText?.match(/file ([a-f0-9]{12})…/)?.[1];
    expect(expectedFileHashPrefix).toBeTruthy();
    const downloadPromise = page.waitForEvent("download");
    const responsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "GET" &&
        new URL(response.url()).pathname.endsWith("/download") &&
        response.status() === 200,
    );
    await article.getByRole("button", { name: "Download" }).click();
    const [download, response] = await Promise.all([downloadPromise, responsePromise]);
    expect(download.suggestedFilename()).toMatch(/\.(pdf|csv|json)$/);
    const downloadPath = await download.path();
    expect(downloadPath).not.toBeNull();
    expect((await stat(downloadPath!)).size).toBeGreaterThan(0);
    const bytes = await readFile(downloadPath!);
    const byteHash = createHash("sha256").update(bytes).digest("hex");
    expect(byteHash.slice(0, 12)).toBe(expectedFileHashPrefix);
    const packetHash = response.headers()["x-packet-snapshot-sha256"];
    expect(packetHash).toMatch(/^[a-f0-9]{64}$/);
    downloadedPacketHashes.push(packetHash);
    if (download.suggestedFilename().endsWith(".json")) {
      const payload = JSON.parse(bytes.toString("utf8")) as {
        snapshot_sha256: string;
        snapshot: unknown;
      };
      expect(payload.snapshot_sha256).toBe(packetHash);
      expect(createHash("sha256").update(JSON.stringify(payload.snapshot)).digest("hex")).toBe(
        packetHash,
      );
    } else {
      expect(bytes.includes(Buffer.from(packetHash))).toBe(true);
    }
  }
  expect(new Set(downloadedPacketHashes).size).toBe(1);
  expect(`packet ${downloadedPacketHashes[0].slice(0, 12)}…`).toBe(packetHashes[0]);

  await page
    .getByLabel("Decision reason")
    .fill("All four approved packet formats share one snapshot and were downloaded for inspection.");
  await page.getByRole("button", { name: "Mark packet exported" }).click();
  await expectCaseStatus(page, "Exported");

  await page
    .getByLabel("Decision reason")
    .fill("Reopen the synthetic evidence review to prove controlled revision recovery.");
  await page.getByRole("button", { name: "Reopen evidence review" }).click();
  const caseHeader = page.locator("main header");
  await expectCaseStatus(page, "Evidence review");
  await expect(caseHeader).toContainText("Revision 2");
  await expect(page.getByRole("heading", { name: matterName })).toBeVisible();

  await page.reload();
  await expect(page.getByRole("heading", { name: matterName })).toBeVisible();
  await expect(page.locator("main header")).toContainText("Revision 2");
  await page.getByRole("button", { name: /^Readiness & packet/ }).click();
  const revisionControls = page.getByRole("complementary", {
    name: "Workflow and revision controls",
  });
  await expect(revisionControls).toContainText("Revision 2");
  await expect(revisionControls).toContainText("Revision 1");
  await page.getByRole("button", { name: /^Audit/ }).click();
  await expect(page.getByText("Case · revision created")).toBeVisible();
  await expect(page.getByText("Fee · superseded")).toBeVisible();
  await expectNoSeriousAccessibilityViolations(page);
});

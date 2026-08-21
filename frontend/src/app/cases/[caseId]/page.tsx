import { AppShell } from "@/components/app-shell";
import { RequireAuth } from "@/components/auth-provider";
import { Workbench } from "@/components/workbench";

export default async function CaseWorkbenchPage({
  params,
}: {
  params: Promise<{ caseId: string }>;
}) {
  const { caseId } = await params;
  return (
    <RequireAuth>
      <AppShell>
        <Workbench caseId={caseId} />
      </AppShell>
    </RequireAuth>
  );
}

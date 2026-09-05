import { PageHeader } from "@/components/layout/AppShell";

export default function ComingSoon({ title, phase }: { title: string; phase: string }) {
  return (
    <>
      <PageHeader title={title} description={`Built in ${phase} of the RecoverAI frontend plan.`} />
      <div className="p-6 md:p-8">
        <div className="rounded-lg border border-dashed p-10 text-center text-sm text-muted-foreground">
          This page isn't built yet — it's coming in {phase}.
        </div>
      </div>
    </>
  );
}

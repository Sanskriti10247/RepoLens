import type { ReactNode } from "react";

import { RepoHeader } from "@/components/RepoNav";

export default async function RepositoryLayout({
  children,
  params,
}: {
  children: ReactNode;
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return (
    <>
      <RepoHeader id={id} />
      {children}
    </>
  );
}

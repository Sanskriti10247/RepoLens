import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import AskPage from "@/app/repositories/[id]/ask/page";
import RepositoriesPage from "@/app/page";
import { resetReadOnlyCache } from "@/lib/useReadOnly";

vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "r1" }),
  useRouter: () => ({ push: vi.fn() }),
}));

function stubApi(readOnly: boolean) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/health") return Response.json({ status: "ok", version: "0", read_only: readOnly });
      return Response.json({ items: [], page: { total: 0, limit: 100, offset: 0 } });
    }),
  );
}

beforeEach(() => resetReadOnlyCache());
afterEach(() => vi.unstubAllGlobals());

describe("read-only instance", () => {
  it("hides the register form and the question form", async () => {
    stubApi(true);
    render(<RepositoriesPage />);
    render(<AskPage />);
    await waitFor(() => expect(screen.getAllByRole("note")).toHaveLength(2));
    expect(screen.queryByRole("button", { name: /register and analyze/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^ask$/i })).toBeNull();
  });

  it("keeps the forms on a normal instance", async () => {
    stubApi(false);
    render(<RepositoriesPage />);
    render(<AskPage />);
    expect(await screen.findByRole("button", { name: /register and analyze/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^ask$/i })).toBeInTheDocument();
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("does not offer actions until the API has answered", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));
    render(<AskPage />);
    fireEvent.change(screen.getByLabelText("Question"), { target: { value: "Who calls x?" } });
    expect(screen.getByRole("button", { name: /^ask$/i })).toBeDisabled();
  });
});

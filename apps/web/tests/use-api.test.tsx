import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useApi } from "@/lib/useApi";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function Probe({ path, poll }: { path: string | null; poll?: boolean }) {
  const { data, error, loading } = useApi<{ status: string }>(path, {
    pollWhile: poll ? (d) => d.status !== "DONE" : undefined,
    pollMs: 10,
  });
  return (
    <div>
      <span data-testid="loading">{String(loading)}</span>
      <span data-testid="data">{data?.status ?? "none"}</span>
      <span data-testid="error">{error?.code ?? "none"}</span>
    </div>
  );
}

describe("useApi", () => {
  it("loads data", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "OK" })));
    render(<Probe path="/thing" />);
    await waitFor(() => expect(screen.getByTestId("data")).toHaveTextContent("OK"));
    expect(screen.getByTestId("loading")).toHaveTextContent("false");
  });

  it("polls until the condition is met", async () => {
    const statuses = ["RUNNING", "RUNNING", "DONE"];
    const fetchMock = vi.fn(async () => Response.json({ status: statuses.shift() ?? "DONE" }));
    vi.stubGlobal("fetch", fetchMock);
    render(<Probe path="/job" poll />);
    await waitFor(() => expect(screen.getByTestId("data")).toHaveTextContent("DONE"));
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("reports API errors with their code", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ error: { code: "not_found", message: "nope" } }, { status: 404 })),
    );
    render(<Probe path="/missing" />);
    await waitFor(() => expect(screen.getByTestId("error")).toHaveTextContent("not_found"));
  });

  it("never shows another path's data", async () => {
    let resolveSecond: (value: Response) => void = () => {};
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(async () => Response.json({ status: "FIRST" }))
      .mockImplementationOnce(() => new Promise<Response>((resolve) => (resolveSecond = resolve)));
    vi.stubGlobal("fetch", fetchMock);
    const { rerender } = render(<Probe path="/a" />);
    await waitFor(() => expect(screen.getByTestId("data")).toHaveTextContent("FIRST"));
    rerender(<Probe path="/b" />);
    expect(screen.getByTestId("data")).toHaveTextContent("none");
    await act(async () => resolveSecond(Response.json({ status: "SECOND" })));
    await waitFor(() => expect(screen.getByTestId("data")).toHaveTextContent("SECOND"));
  });

  it("skips the request for a null path", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<Probe path={null} />);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByTestId("loading")).toHaveTextContent("false");
  });
});

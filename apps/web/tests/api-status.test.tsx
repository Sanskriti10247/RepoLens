import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiStatus } from "@/components/ApiStatus";

afterEach(() => vi.unstubAllGlobals());

describe("ApiStatus", () => {
  it("shows ready when the API reports ready", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "ready", checks: [] })));
    render(<ApiStatus pollMs={60000} />);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("API ready"));
  });

  it("shows degraded when the API is up but not ready", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ status: "not_ready", checks: [] }, { status: 503 })),
    );
    render(<ApiStatus pollMs={60000} />);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("not ready"));
  });

  it("shows unreachable when the proxy cannot reach the API", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ error: { code: "api_unreachable" } }, { status: 502 })),
    );
    render(<ApiStatus pollMs={60000} wakeMs={0} />);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("API unreachable"));
  });

  it("says the API is waking while a sleeping instance starts", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ error: { code: "api_unreachable" } }, { status: 502 })),
    );
    render(<ApiStatus pollMs={60000} />);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Waking the API"));
  });
});

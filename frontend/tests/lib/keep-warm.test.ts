// Keep-warm heartbeat tests.
//
// Covers the lifecycle contract for startKeepWarm():
//   - Pings immediately on start, then every 4 minutes.
//   - Pauses while ``document.hidden`` is true.
//   - Resumes on visibilitychange -> visible.
//   - Stops on ``auth:unauthenticated`` window event.
//   - Cleanup function tears down timers and listeners.

import { startKeepWarm } from "@/lib/keep-warm";

const FOUR_MIN_MS = 4 * 60 * 1000;

describe("startKeepWarm", () => {
  const fetchMock = vi.fn<typeof fetch>();
  let visibilityValue: "visible" | "hidden" = "visible";

  beforeEach(() => {
    fetchMock.mockReset();
    fetchMock.mockResolvedValue(new Response(null, { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    visibilityValue = "visible";
    // jsdom's document.hidden / visibilityState are read-only via the
    // property descriptor; redefine them for the duration of each test.
    Object.defineProperty(document, "hidden", {
      configurable: true,
      get: () => visibilityValue === "hidden",
    });
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => visibilityValue,
    });
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  function fireVisibilityChange(next: "visible" | "hidden") {
    visibilityValue = next;
    document.dispatchEvent(new Event("visibilitychange"));
  }

  it("pings /health?keep-warm=1 immediately on mount", () => {
    const stop = startKeepWarm();
    try {
      expect(fetchMock).toHaveBeenCalledTimes(1);
      const [url, init] = fetchMock.mock.calls[0];
      // Backend exposes /health (not /api/v1/health); nginx + App Platform
      // already route it. ``?keep-warm=1`` lets future access-log
      // tooling distinguish heartbeats from real liveness probes.
      expect(url).toBe("/health?keep-warm=1");
      expect((init as RequestInit | undefined)?.method).toBe("GET");
      expect((init as RequestInit | undefined)?.credentials).toBe("omit");
    } finally {
      stop();
    }
  });

  it("pings again every 4 minutes via setInterval", async () => {
    const stop = startKeepWarm();
    try {
      expect(fetchMock).toHaveBeenCalledTimes(1); // immediate

      await vi.advanceTimersByTimeAsync(FOUR_MIN_MS);
      expect(fetchMock).toHaveBeenCalledTimes(2);

      await vi.advanceTimersByTimeAsync(FOUR_MIN_MS);
      expect(fetchMock).toHaveBeenCalledTimes(3);

      await vi.advanceTimersByTimeAsync(FOUR_MIN_MS);
      expect(fetchMock).toHaveBeenCalledTimes(4);
    } finally {
      stop();
    }
  });

  it("does not advance ticks before 4 minutes have elapsed", async () => {
    const stop = startKeepWarm();
    try {
      expect(fetchMock).toHaveBeenCalledTimes(1);

      await vi.advanceTimersByTimeAsync(FOUR_MIN_MS - 1);
      expect(fetchMock).toHaveBeenCalledTimes(1); // still just the immediate ping

      await vi.advanceTimersByTimeAsync(1);
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {
      stop();
    }
  });

  it("stops pinging when document becomes hidden", async () => {
    const stop = startKeepWarm();
    try {
      expect(fetchMock).toHaveBeenCalledTimes(1); // immediate

      fireVisibilityChange("hidden");

      // Advance well past several intervals while hidden -- no ping.
      await vi.advanceTimersByTimeAsync(FOUR_MIN_MS * 3);
      expect(fetchMock).toHaveBeenCalledTimes(1);
    } finally {
      stop();
    }
  });

  it("resumes pinging on visibilitychange back to visible", async () => {
    const stop = startKeepWarm();
    try {
      expect(fetchMock).toHaveBeenCalledTimes(1); // immediate on mount

      fireVisibilityChange("hidden");
      await vi.advanceTimersByTimeAsync(FOUR_MIN_MS * 2);
      expect(fetchMock).toHaveBeenCalledTimes(1); // still paused

      fireVisibilityChange("visible");
      // Resumes with an immediate ping.
      expect(fetchMock).toHaveBeenCalledTimes(2);

      // And starts a new 4-min interval.
      await vi.advanceTimersByTimeAsync(FOUR_MIN_MS);
      expect(fetchMock).toHaveBeenCalledTimes(3);
    } finally {
      stop();
    }
  });

  it("stops permanently on auth:unauthenticated — no resume on later visibility change (architect P2 on PR #309)", async () => {
    // The "stops permanently" contract is what the test name promised.
    // Earlier the implementation only stopped the timer on unauth but
    // left the visibilitychange listener attached, so a later
    // hidden→visible transition would call start() and ping anyway.
    // Architect P2 added a ``stoppedPermanently`` flag so start() is a
    // no-op after the terminal signal; this test pins the corrected
    // contract.
    const stop = startKeepWarm();
    try {
      expect(fetchMock).toHaveBeenCalledTimes(1);

      window.dispatchEvent(new Event("auth:unauthenticated"));

      await vi.advanceTimersByTimeAsync(FOUR_MIN_MS * 3);
      expect(fetchMock).toHaveBeenCalledTimes(1); // never pinged again

      // The architect P2 assertion: hidden → visible after unauth must
      // NOT pump out a new ping. Before the fix, ``start()`` ran and
      // a fetch fired. After the fix, ``stoppedPermanently`` short-
      // circuits ``start()``. The next sign-in mounts a fresh
      // keep-warm via AppShell's effect remount, which has its own
      // ``stoppedPermanently=false`` closure.
      fireVisibilityChange("hidden");
      fireVisibilityChange("visible");
      await vi.advanceTimersByTimeAsync(FOUR_MIN_MS * 3);
      expect(fetchMock).toHaveBeenCalledTimes(1);
    } finally {
      stop();
    }
  });

  it("cleanup function clears the interval and removes listeners", async () => {
    const stop = startKeepWarm();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    stop();

    // No further pings on interval.
    await vi.advanceTimersByTimeAsync(FOUR_MIN_MS * 3);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // visibilitychange listener should be removed -- toggling hidden ->
    // visible must NOT re-start the timer.
    fireVisibilityChange("hidden");
    fireVisibilityChange("visible");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // auth:unauthenticated listener should also be removed.
    window.dispatchEvent(new Event("auth:unauthenticated"));
    await vi.advanceTimersByTimeAsync(FOUR_MIN_MS * 2);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("swallows fetch errors silently (best-effort heartbeat)", async () => {
    fetchMock.mockReset();
    fetchMock.mockRejectedValue(new TypeError("network down"));
    const stop = startKeepWarm();
    try {
      // Immediate ping rejected; promise must not surface unhandled.
      // We advance a tick to let the microtask flush.
      await vi.advanceTimersByTimeAsync(0);
      expect(fetchMock).toHaveBeenCalledTimes(1);

      // Next tick still fires despite the previous rejection.
      await vi.advanceTimersByTimeAsync(FOUR_MIN_MS);
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {
      stop();
    }
  });
});

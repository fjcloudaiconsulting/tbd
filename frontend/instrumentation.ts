/**
 * Next.js instrumentation — runs once on server startup.
 * Logs the server start event in structured JSON format.
 */
export async function register() {
  if (process.env.NEXT_RUNTIME === "nodejs") {
    const entry = {
      timestamp: new Date().toISOString(),
      level: "info",
      logger: "frontend",
      event: "starting",
      app: "PFV2 Frontend",
      env: process.env.NODE_ENV,
      runtime: "nodejs",
    };
    process.stdout.write(JSON.stringify(entry) + "\n");
  }
}

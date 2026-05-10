import {
  clearPersisted,
  readPersisted,
  writePersisted,
} from "@/lib/persisted-state";

const KEY = "pfv:test:raw";

beforeEach(() => {
  window.localStorage.clear();
});

describe("persisted-state helpers", () => {
  it("readPersisted returns the fallback when the key is absent", () => {
    expect(readPersisted(KEY, { a: 1 })).toEqual({ a: 1 });
  });

  it("readPersisted returns the parsed JSON when present", () => {
    window.localStorage.setItem(KEY, JSON.stringify({ a: 2 }));
    expect(readPersisted(KEY, { a: 1 })).toEqual({ a: 2 });
  });

  it("readPersisted returns the fallback on malformed JSON", () => {
    window.localStorage.setItem(KEY, "{nope");
    expect(readPersisted(KEY, { a: 1 })).toEqual({ a: 1 });
  });

  it("readPersisted runs the validator and rejects bad shapes", () => {
    window.localStorage.setItem(KEY, JSON.stringify({ a: "string" }));
    type Shape = { a: number };
    const isShape = (v: unknown): v is Shape =>
      typeof v === "object" &&
      v !== null &&
      typeof (v as Record<string, unknown>).a === "number";
    expect(readPersisted<Shape>(KEY, { a: 1 }, isShape)).toEqual({ a: 1 });
  });

  it("writePersisted stores a JSON-encoded value", () => {
    writePersisted(KEY, { a: 3 });
    expect(JSON.parse(window.localStorage.getItem(KEY)!)).toEqual({ a: 3 });
  });

  it("clearPersisted removes the key", () => {
    writePersisted(KEY, { a: 3 });
    clearPersisted(KEY);
    expect(window.localStorage.getItem(KEY)).toBeNull();
  });
});

/**
 * Phone lookup module — TypeScript port of the reference Python `PhoneLookup` MCP server.
 *
 * The module is intentionally provider-agnostic: each upstream returns a slightly
 * different JSON shape, so we normalize every record into a `Record<string, string>`
 * before handing the result back to the caller / renderer.
 */

/** A single normalized lookup record (key → value). */
export type PhoneRecord = Record<string, string>;

/** The normalized lookup result returned to the Pi tool and renderer. */
export interface PhoneLookupResult {
  /** The cleaned phone number that was actually queried. */
  phone: string;
  /** Which provider produced the result (1-based index, matching README). */
  provider: number;
  /** Non-empty list of normalized records. */
  records: PhoneRecord[];
  /** Total number of records returned by the winning provider. */
  count: number;
}

const COMMON_HEADERS: Record<string, string> = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
};

/** Timeout (ms) for every upstream request — matches the Python reference. */
const REQUEST_TIMEOUT_MS = 8000;

/**
 * Clean a raw phone input: keep digits only, then strip a leading `91` country code
 * if the result is longer than 10 digits.
 */
export function cleanPhone(input: string): string {
  const digits = String(input ?? "")
    .split("")
    .filter((c) => c >= "0" && c <= "9")
    .join("");
  if (digits.length > 10 && digits.startsWith("91")) {
    return digits.slice(2);
  }
  return digits;
}

/** Coerce any JSON value into a normalized `Record<string, string>`. */
function normalizeRecord(raw: unknown): PhoneRecord | null {
  if (raw === null || raw === undefined) return null;

  if (typeof raw === "object" && !Array.isArray(raw)) {
    const out: PhoneRecord = {};
    for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
      const valStr =
        v === null || v === undefined
          ? "N/A"
          : String(v).replace(/!/g, " ").trim() || "N/A";
      out[String(k)] = valStr;
    }
    return Object.keys(out).length > 0 ? out : null;
  }

  if (typeof raw === "string" || typeof raw === "number" || typeof raw === "boolean") {
    return { value: String(raw) };
  }

  return null;
}

/** Coerce an arbitrary upstream JSON payload into a non-empty list of records. */
function toRecords(raw: unknown): PhoneRecord[] | null {
  if (raw === null || raw === undefined) return null;

  // Common shapes: data.result, data.results, data.results as object
  let list: unknown[] | null = null;

  if (Array.isArray(raw)) {
    list = raw;
  } else if (typeof raw === "object") {
    const obj = raw as Record<string, unknown>;

    if (Array.isArray(obj.result)) {
      list = obj.result as unknown[];
    } else if (Array.isArray(obj.results)) {
      list = obj.results as unknown[];
    } else if (obj.results && typeof obj.results === "object" && !Array.isArray(obj.results)) {
      // Some providers return results as a dict-of-records.
      list = Object.values(obj.results as Record<string, unknown>);
    } else if (obj.data && Array.isArray((obj.data as { result?: unknown[] }).result)) {
      list = (obj.data as { result: unknown[] }).result;
    }
  }

  if (!list || list.length === 0) return null;

  const records: PhoneRecord[] = [];
  for (const item of list) {
    const rec = normalizeRecord(item);
    if (rec) records.push(rec);
  }
  return records.length > 0 ? records : null;
}

async function fetchJson(url: string, init: RequestInit): Promise<unknown> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const res = await fetch(url, { ...init, signal: controller.signal });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status} ${res.statusText}`);
    }
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

/* ------------------------------------------------------------------ *
 * Provider 1 — Supabase Edge Function (POST JSON)
 * ------------------------------------------------------------------ */
async function fetchApi1(phone: string): Promise<PhoneRecord[] | null> {
  const url =
    "https://ltifkzmhynhaawutlcjt.supabase.co/functions/v1/bright-service";
  const data = await fetchJson(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...COMMON_HEADERS },
    body: JSON.stringify({ mobile: phone, accessKey: "pmsecurity" }),
  });
  // Reference: data.result is the record list.
  const records =
    (data as { result?: unknown })?.result !== undefined
      ? toRecords(data)
      : toRecords(data);
  return records;
}

/* ------------------------------------------------------------------ *
 * Provider 2 — numtoinfo worker (GET, success flag)
 * ------------------------------------------------------------------ */
async function fetchApi2(phone: string): Promise<PhoneRecord[] | null> {
  const url = `https://numtoinfo.fuckyoubitch.workers.dev/search?q=${encodeURIComponent(phone)}`;
  const data = (await fetchJson(url, { method: "GET", headers: COMMON_HEADERS })) as {
    success?: boolean;
    results?: unknown;
  };
  if (!data?.success) return null;
  return toRecords(data);
}

/* ------------------------------------------------------------------ *
 * Provider 3 — techvishalboss lookup (GET, status flag, results as dict)
 * ------------------------------------------------------------------ */
async function fetchApi3(phone: string): Promise<PhoneRecord[] | null> {
  const url =
    `https://techvishalboss.com/api/v1/lookup.php` +
    `?key=TVB_SGL_EBB13EBC&service=number&number=${encodeURIComponent(phone)}`;
  const data = (await fetchJson(url, { method: "GET", headers: COMMON_HEADERS })) as {
    status?: boolean | string;
    results?: unknown;
  };
  // Reference treats `status` as a truthy flag (bool or non-empty string).
  const ok = typeof data?.status === "string" ? data.status !== "" : Boolean(data?.status);
  if (!ok) return null;
  return toRecords(data);
}

/* ------------------------------------------------------------------ *
 * Provider 4 — rootx-osint (GET, success flag, result list)
 * ------------------------------------------------------------------ */
async function fetchApi4(phone: string): Promise<PhoneRecord[] | null> {
  const url =
    `https://rootx-osint.in/?type=num&key=${encodeURIComponent("@llx_oIl")}` +
    `&query=${encodeURIComponent(phone)}`;
  const data = (await fetchJson(url, { method: "GET", headers: COMMON_HEADERS })) as {
    success?: boolean;
    status?: string;
    result?: unknown;
  };
  const ok = data?.success === true || data?.status === "success";
  if (!ok) return null;
  return toRecords(data);
}

/** Ordered list of provider fetchers — index 0 == provider #1 in docs. */
const PROVIDERS: Array<{ name: string; run: (phone: string) => Promise<PhoneRecord[] | null> }> = [
  { name: "bright-service", run: fetchApi1 },
  { name: "numtoinfo", run: fetchApi2 },
  { name: "techvishalboss", run: fetchApi3 },
  { name: "rootx-osint", run: fetchApi4 },
];

export interface LookupOutcome {
  ok: boolean;
  result?: PhoneLookupResult;
  /** When `ok === false`, a human-readable reason. */
  reason?: string;
}

/**
 * Run the lookup pipeline: clean → try each provider in order → return the first
 * non-empty result set. Any provider error (network, non-200, bad JSON, empty)
 * is swallowed so the next provider can be tried.
 */
export async function lookupPhone(rawInput: string): Promise<LookupOutcome> {
  const phone = cleanPhone(rawInput);
  if (!phone || phone.length < 10) {
    return {
      ok: false,
      reason: `Invalid phone number '${rawInput}'. Please provide a valid 10-digit number.`,
    };
  }

  for (let i = 0; i < PROVIDERS.length; i++) {
    const provider = PROVIDERS[i];
    try {
      const records = await provider.run(phone);
      if (records && records.length > 0) {
        return {
          ok: true,
          result: {
            phone,
            provider: i + 1,
            records,
            count: records.length,
          },
        };
      }
    } catch {
      // Best-effort: swallow and try the next provider.
      continue;
    }
  }

  return {
    ok: false,
    reason: `No details found for phone number '${phone}' across all available servers.`,
  };
}

/** Render a single record as a Markdown-style bullet list for the textual content. */
export function recordsToText(phone: string, records: PhoneRecord[]): string {
  const lines: string[] = [`### Lookup Results for \`${phone}\``, ""];
  records.forEach((rec, idx) => {
    lines.push(`**Record #${idx + 1}:**`);
    for (const [k, v] of Object.entries(rec)) {
      lines.push(`* **${k}:** ${v}`);
    }
    lines.push("");
  });
  return lines.join("\n");
}

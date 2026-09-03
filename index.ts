import {
  defineTool,
  type ExtensionAPI,
  type ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import { Container, Text } from "@earendil-works/pi-tui";
import { Type } from "@sinclair/typebox";
import {
  lookupPhone,
  recordsToText,
  type PhoneLookupResult,
} from "./phone-lookup.js";

/** Details carried by an `arix-phone` message — used by the renderer. */
type PhoneDetails =
  | (PhoneLookupResult & { kind: "found" })
  | { kind: "not_found"; phone: string; reason: string };

function phoneIcon(): string {
  return "📞";
}

function result(details: PhoneDetails) {
  const text =
    details.kind === "found"
      ? recordsToText(details.phone, details.records)
      : `### Lookup completed\n\n${details.reason}`;
  return {
    content: [{ type: "text" as const, text }],
    details,
  };
}

export default function register(pi: ExtensionAPI) {
  /* ---------------------------- renderer ---------------------------- */
  pi.registerMessageRenderer<PhoneDetails>(
    "arix-phone",
    (message, { expanded }, theme) => {
      const d = message.details;
      if (!d) return undefined;

      const box = new Container();

      if (d.kind === "not_found") {
        box.addChild(new Text(theme.bold(`${phoneIcon()} No results`), 0, 0));
        box.addChild(new Text(theme.fg("dim", `  ${d.reason}`), 0, 0));
        return box;
      }

      box.addChild(
        new Text(theme.bold(`${phoneIcon()} ${d.phone}`), 0, 0)
      );
      box.addChild(
        new Text(
          theme.fg("dim", `  provider #${d.provider} · ${d.count} record(s)`),
          0, 0
        )
      );

      const previewCount = expanded ? d.records.length : Math.min(d.records.length, 3);

      for (let i = 0; i < previewCount; i++) {
        const rec = d.records[i];
        const entries = Object.entries(rec);
        const firstVal = entries.length > 0 ? entries[0][1] : "—";
        const nameKey =
          entries.find(([k]) => /name/i.test(k))?.[1] ?? firstVal;
        box.addChild(
          new Text(theme.fg("accent", `  • ${nameKey}`), 0, 0)
        );
        if (entries.length > 1) {
          const subKey = entries.find(([k]) => /addr|location|city|state/i.test(k));
          if (subKey) {
            box.addChild(new Text(theme.fg("dim", `      ${subKey[1]}`), 0, 0));
          }
        }
      }

      if (!expanded && d.records.length > 3) {
        box.addChild(
          new Text(theme.fg("dim", `  … +${d.records.length - 3} more`), 0, 0)
        );
      }

      box.addChild(
        new Text(
          theme.fg("accent", "  [Copy]") +
            theme.fg("accent", " [Export]") +
            theme.fg("accent", " [Re-lookup]"),
          0, 0
        )
      );

      return box;
    }
  );

  /* ------------------------------ tool ------------------------------ */
  pi.registerTool(defineTool({
    name: "lookup_phone",
    label: "Phone Lookup",
    description:
      "Lookup owner and caller details for a given mobile number using automatic multi-server fallback.",
    promptSnippet: "Look up phone number owner / caller details.",
    parameters: Type.Object({
      phone_number: Type.String({
        description:
          "Phone number to look up. Non-digit characters are stripped; a leading 91 country code is removed automatically.",
      }),
    }),
    execute: async (
      _toolCallId,
      params,
      _signal,
      _onUpdate,
      _ctx: ExtensionContext
    ) => {
      const outcome = await lookupPhone(params.phone_number);

      if (!outcome.ok || !outcome.result) {
        return result({
          kind: "not_found",
          phone: params.phone_number,
          reason: outcome.reason ?? "No details found.",
        });
      }

      return result({ kind: "found", ...outcome.result });
    },
  }));
}

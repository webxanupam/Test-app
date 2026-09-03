const APIS = [
  {
    name: "Tata",
    url: "http://localhost:6081/test/otp/a",
    method: "POST",
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
    url: "http://localhost:6081/test/otp/a",
    method: "POST",
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

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function buildBody(
  body: Record<string, unknown>,
  phone: string,
): Record<string, unknown> {
  return JSON.parse(
    JSON.stringify(body).replaceAll("{phone}", phone),
  );
}

async function callApi(
  api: (typeof APIS)[number],
  phone: string,
) {
  try {
    const response = await fetch(api.url, {
      method: api.method,
      headers: api.headers,
      body: JSON.stringify(buildBody(api.body, phone)),
    });

    const responseText = await response.text();

    return {
      provider: api.name,
      status: response.status,
      success: (api.success_codes as readonly number[]).includes(
        response.status,
      ),
      response: responseText.slice(0, 1000),
    };
  } catch (error) {
    return {
      provider: api.name,
      status: 0,
      success: false,
      response:
        error instanceof Error
          ? error.message
          : String(error),
    };
  }
}

export default function register(pi) {
  pi.registerTool({
    name: "send_otp",
    label: "Send OTP",

    description:
      "Send a requested number of OTP requests using the configured local test providers. " +
      "Interpret the user's requested OTP count automatically. " +
      "For example, 'send 1 OTP' means count=1 and 'send 2 OTPs' means count=2. " +
      "Requests are sent sequentially with the configured time gap.",

    parameters: {
      type: "object",
      properties: {
        phone: {
          type: "string",
          description: "Phone number.",
        },

        count: {
          type: "integer",
          minimum: 1,
          maximum: 2,
          description:
            "Number of OTP requests/providers to use. " +
            "The agent should infer this from natural language. " +
            "1 means first provider only; 2 means both providers.",
        },

        time_gap: {
          type: "number",
          minimum: 0,
          maximum: 60,
          default: 3,
          description:
            "Delay in seconds between provider requests. " +
            "Default is 3 seconds.",
        },
      },

      required: ["phone", "count"],
      additionalProperties: false,
    },

    async execute(_toolCallId, params) {
      const phone = normalizePhone(params.phone);

      if (!phone) {
        return {
          content: [
            {
              type: "text",
              text: "Invalid phone number.",
            },
          ],
          isError: true,
        };
      }

      const count = Math.min(
        Math.max(Number(params.count) || 1, 1),
        APIS.length,
      );

      const timeGap = Math.min(
        Math.max(Number(params.time_gap ?? 3), 0),
        60,
      );

      const selectedApis = APIS.slice(0, count);
      const results = [];

      for (let i = 0; i < selectedApis.length; i++) {
        if (i > 0 && timeGap > 0) {
          await sleep(timeGap * 1000);
        }

        const result = await callApi(
          selectedApis[i],
          phone,
        );

        results.push(result);
      }

      const successful = results.filter(
        (result) => result.success,
      ).length;

      const output = results
        .map(
          (result, index) =>
            `${index + 1}. ${result.provider}: ` +
            `${result.success ? "SUCCESS" : "FAILED"} ` +
            `(HTTP ${result.status})\n` +
            `${result.response}`,
        )
        .join("\n\n");

      return {
        content: [
          {
            type: "text",
            text:
              `Requested: ${count} OTP request(s)\n` +
              `Completed: ${results.length}\n` +
              `Successful: ${successful}\n` +
              `Time gap: ${timeGap}s\n\n` +
              output,
          },
        ],

        details: {
          phone,
          requestedCount: count,
          timeGap,
          results,
        },
      };
    },
  });
}
